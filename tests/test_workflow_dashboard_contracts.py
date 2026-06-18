"""Workflow and dashboard release-blocker contracts."""
from __future__ import annotations

import json
import sqlite3
import textwrap
import threading
import urllib.error
import urllib.request
from pathlib import Path

from agentic_os.dashboard import build_overview
from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db
from agentic_os.time_utils import now_iso


_CFG = textwrap.dedent(
    """\
    runtime:
      root: .agentic-os
      timezone: Europe/Warsaw
      max_parallel_tasks: 1
      heartbeat_seconds: 20
      lease_ttl_seconds: 60
      stale_lease_seconds: 90
      shutdown_grace_seconds: 1
      timeouts:
        default_seconds: 30
        docker_seconds: 30
        test_seconds: 30
        model_seconds: 30
        report_seconds: 30
    sut:
      root: .
      compose_file: docker-compose.yml
      compose_project_name: agentic-os-sut
      autostart: false
      healthcheck:
        command: ["true"]
        timeout_seconds: 1
        retries: 0
      test_runner: ./run-tests.sh
      install_shim_allowed: false
    models:
      planner: {provider: claude, command: ["claude"], role: opus}
      implementer: {provider: claude, command: ["claude"], role: sonnet}
      reviewer: {provider: codex, command: ["codex"], role: codex}
    budgets:
      fail_mode: abort
      session:
        max_tokens: 100
        max_usd: 1
    dashboard:
      host: 127.0.0.1
      port: 8765
      enable_write_endpoints: false
    paths:
      reports: reports
      bugs: bugs
      evidence: evidence
      prompts: .qualitycat/prompts
    reports:
      copy_reports_script: scripts/copy-reports.sh
      extract_last_run_script: scripts/extract-last-run.sh
      build_summary_script: scripts/build-summary.sh
      require_reports_on_failure: true
    gates:
      known_bugs_fail_exit: true
      assertion_changes_require_decision: true
      exact_spec_failure_opens_bug: true
      require_functional_area_tag: true
      require_lifecycle_tag: true
      infrastructure_exit_code: 2
    """
)


def _runtime(tmp_path: Path) -> tuple[sqlite3.Connection, RuntimePaths, EventLog, Orchestrator]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    return conn, paths, events, orch


def _write_runner_fixture(repo: Path) -> None:
    (repo / ".qualitycat").mkdir(parents=True, exist_ok=True)
    (repo / ".qualitycat" / "agentic-os.yml").write_text(_CFG, encoding="utf-8")

    def write_exec(rel: str, body: str) -> None:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    write_exec("run-tests.sh", "#!/usr/bin/env bash\nexit 0\n")
    write_exec("scripts/copy-reports.sh", "#!/usr/bin/env bash\nmkdir -p reports\nexit 0\n")
    write_exec(
        "scripts/extract-last-run.sh",
        "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p reports\n"
        "cat > reports/last-run.json <<'JSON'\n"
        + json.dumps(
            {
                "total": 1,
                "passed": 1,
                "failed": 0,
                "skipped": 0,
                "failures": [],
            }
        )
        + "\nJSON\n",
    )
    write_exec(
        "scripts/build-summary.sh",
        "#!/usr/bin/env bash\nmkdir -p reports\nprintf '# ok\\n' > reports/summary.md\n",
    )


def _seed_work_item(conn: sqlite3.Connection, paths: RuntimePaths, wid: str) -> None:
    spec = paths.task_specs_dir / f"{wid}.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# Task\n", encoding="utf-8")
    now = now_iso()
    conn.execute(
        """
        INSERT INTO work_items(id, title, status, spec_path, sut_root, priority, created_at, updated_at)
        VALUES (?, 'Task', 'planned', ?, '.', 'P1', ?, ?);
        """,
        (wid, str(spec.relative_to(paths.repo_root)), now, now),
    )


def test_run_tests_replays_existing_idempotency_key_for_work_item(tmp_path: Path) -> None:
    from agentic_os.workflows import run_tests

    conn, paths, events, orch = _runtime(tmp_path)
    try:
        _write_runner_fixture(paths.repo_root)
        wid = "TASK-20260526-000000-01HXREFRACTOR-idempotency"
        _seed_work_item(conn, paths, wid)

        first = run_tests(orch, paths, events, work_item_id=wid)
        second = run_tests(orch, paths, events, work_item_id=wid)

        assert first.run_id == second.run_id
        rows = conn.execute(
            "SELECT idempotency_key FROM runs WHERE idempotency_key IS NOT NULL;"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["idempotency_key"].startswith("run-tests:")
        assert any(e["kind"] == "run_tests.idempotent_replay" for e in events.tail(20))
    finally:
        conn.close()


def test_review_gate_respects_active_reviewer_lease(tmp_path: Path) -> None:
    from agentic_os.workflows import run_review_gate

    conn, paths, events, orch = _runtime(tmp_path)
    try:
        _write_runner_fixture(paths.repo_root)
        wid = "TASK-20260526-000000-01HXREFRACTOR-lease"
        _seed_work_item(conn, paths, wid)
        conn.execute(
            """
            UPDATE work_items
               SET reviewer_lease='other', reviewer_lease_expires='2999-01-01T00:00:00.000Z'
             WHERE id=?;
            """,
            (wid,),
        )

        result = run_review_gate(orch, paths, events, diff_path=None, scope="all", work_item_id=wid)

        assert result.ok is False
        assert result.exit_code == 2
        assert result.failure_kind == "infra"
        row = conn.execute("SELECT reviewer_lease FROM work_items WHERE id=?;", (wid,)).fetchone()
        assert row["reviewer_lease"] == "other"
        assert any(e["kind"] == "gate.review_lease_busy" for e in events.tail(20))
    finally:
        conn.close()


def test_dashboard_overview_includes_budget_usage(tmp_path: Path) -> None:
    conn, paths, _events, _orch = _runtime(tmp_path)
    try:
        _write_runner_fixture(paths.repo_root)
        conn.execute(
            """
            INSERT INTO model_invocations(
              id, session_id, model_role, provider, command, started_at,
              tokens_in, tokens_out, cost_usd
            ) VALUES (
              'MODEL-1', 'S1', 'codex', 'codex', '["codex"]', ?, 70, 20, 0.25
            );
            """,
            (now_iso(),),
        )

        payload = build_overview(conn, paths)

        assert payload["budget"]["total_tokens"] == 90
        assert payload["budget"]["state"] == "warn"
        assert payload["budget"]["limits"]["max_tokens"] == 100
    finally:
        conn.close()


def test_dashboard_returns_405_with_allow_header_for_known_route(tmp_path: Path) -> None:
    conn, paths, _events, _orch = _runtime(tmp_path)
    server = make_server(paths, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/healthz"
        req = urllib.request.Request(url, data=b"", method="POST")
        try:
            urllib.request.urlopen(req, timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 405
            assert exc.headers["Allow"] == "GET"
            body = json.loads(exc.read().decode("utf-8"))
            assert body["error"] == "method_not_allowed"
        else:  # pragma: no cover
            raise AssertionError("POST /healthz unexpectedly succeeded")
    finally:
        server.shutdown()
        server.server_close()
        conn.close()
