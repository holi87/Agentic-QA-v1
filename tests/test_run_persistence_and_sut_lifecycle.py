"""Run persistence, evidence recording, triage totals, and SUT lifecycle regressions."""
from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path

from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db


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


def _install_runner_and_reports(repo: Path, *, last_run: dict) -> None:
    cfg = repo / ".qualitycat" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_CFG, encoding="utf-8")

    def _x(rel: str, body: str) -> None:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)

    _x(
        "run-tests.sh",
        "#!/usr/bin/env bash\nexit 1\n",
    )
    _x(
        "scripts/copy-reports.sh",
        "#!/usr/bin/env bash\nmkdir -p reports\nexit 0\n",
    )
    _x(
        "scripts/extract-last-run.sh",
        "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p reports\ncat > reports/last-run.json <<'JSON'\n"
        + json.dumps(last_run, indent=2)
        + "\nJSON\nexit 0\n",
    )
    _x(
        "scripts/build-summary.sh",
        "#!/usr/bin/env bash\nmkdir -p reports\nprintf '# stub\\n' > reports/summary.md\nexit 0\n",
    )


def test_triage_summary_carries_run_totals_on_green_run(tmp_path: Path) -> None:
    """Issue #75 — triage on a green run reports run totals, not just
    failure-only categories."""
    from agentic_os.workflows import run_tests

    conn, paths, events, orch = _runtime(tmp_path)
    try:
        _install_runner_and_reports(
            paths.repo_root,
            last_run={
                "total": 5,
                "passed": 5,
                "failed": 0,
                "skipped": 0,
                "failures": [],
            },
        )
        # Force the runner to exit 0 so the report is treated as green.
        (paths.repo_root / "run-tests.sh").write_text(
            "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
        )
        run_tests(orch, paths, events)
        triage_path = next(
            (paths.runtime_root / "runs").glob("*/triage.json")
        )
        payload = json.loads(triage_path.read_text(encoding="utf-8"))
        summary = payload["summary"]
        assert summary["run_total"] == 5
        assert summary["run_passed"] == 5
        assert summary["run_failed"] == 0
        assert summary["failure_total"] == 0
        # Backwards-compat: `pass` / `total` no longer report 0 on a
        # green run.
        assert summary["pass"] == 5
        assert summary["total"] == 5
    finally:
        conn.close()


def test_run_tests_persists_test_results_and_evidence_rows(tmp_path: Path) -> None:
    """Issue #103 — failing scenarios persist as `test_results` rows
    and linked evidence as `evidence` rows."""
    from agentic_os.workflows import run_tests

    conn, paths, events, orch = _runtime(tmp_path)
    try:
        _install_runner_and_reports(
            paths.repo_root,
            last_run={
                "total": 1,
                "passed": 0,
                "failed": 1,
                "skipped": 0,
                "failures": [
                    {
                        "scenario": "checkout rejects invalid card",
                        "classname": "checkout",
                        "tags": ["@functional-orders", "@regression"],
                        "error_message": "expected 422 got 200",
                        "junit_xml": "reports/junit/TEST-fake.xml",
                    }
                ],
            },
        )
        (paths.repo_root / "reports" / "junit").mkdir(parents=True, exist_ok=True)
        (paths.repo_root / "reports" / "junit" / "TEST-fake.xml").write_text(
            "<testsuite/>", encoding="utf-8"
        )
        run_tests(orch, paths, events)

        rows = list(
            conn.execute(
                "SELECT scenario_name, functional_tag, lifecycle_tag, status FROM test_results;"
            ).fetchall()
        )
        assert any(
            r["scenario_name"] == "checkout rejects invalid card"
            and r["functional_tag"] == "@functional-orders"
            and r["lifecycle_tag"] == "@regression"
            and r["status"] == "failed"
            for r in rows
        )
        evidence_rows = list(
            conn.execute(
                "SELECT path, kind, size_bytes FROM evidence;"
            ).fetchall()
        )
        # At least the last-run.json + junit summary should be linked.
        assert any(r["path"] == "reports/last-run.json" for r in evidence_rows)
    finally:
        conn.close()


def test_sut_lifecycle_failure_finalizes_task_state(tmp_path: Path) -> None:
    """Codex review on #130 — when SUT lifecycle aborts the run, the
    task must transition through `finish_task` so the queue is not
    left with a stuck `running` row and status/recovery views stay
    consistent."""
    from agentic_os.workflows import run_tests

    conn, paths, events, orch = _runtime(tmp_path)
    try:
        cfg = paths.repo_root / ".qualitycat" / "agentic-os.yml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(_CFG.replace("autostart: false", "autostart: true"), encoding="utf-8")
        (paths.repo_root / "docker-compose.yml").write_text(
            "services: {}\n", encoding="utf-8"
        )
        for name, body in {
            "run-tests.sh": "#!/usr/bin/env bash\nexit 0\n",
            "scripts/copy-reports.sh": "#!/usr/bin/env bash\nmkdir -p reports\nexit 0\n",
            "scripts/extract-last-run.sh": "#!/usr/bin/env bash\nmkdir -p reports\necho '{\"total\":0,\"passed\":0,\"failed\":0,\"skipped\":0,\"failures\":[],\"discovery_only\":true}' > reports/last-run.json\n",
            "scripts/build-summary.sh": "#!/usr/bin/env bash\nmkdir -p reports\nprintf '# stub\\n' > reports/summary.md\n",
        }.items():
            p = paths.repo_root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
            p.chmod(0o755)

        result = run_tests(orch, paths, events)
        assert result.exit_code == 2
        # Task must be terminal — no `running` rows should remain.
        rows = list(conn.execute("SELECT id, status FROM tasks;").fetchall())
        for r in rows:
            assert r["status"] in {"failed", "succeeded", "cancelled", "timeout"}, dict(r)
        # And the run row that anchors the SUT failure manifest must exist.
        run_rows = list(
            conn.execute("SELECT id, exit_code FROM runs WHERE id = ?;", (result.run_id,)).fetchall()
        )
        assert run_rows and run_rows[0]["exit_code"] == 2
    finally:
        conn.close()


def test_run_tests_aborts_with_infra_exit_when_compose_file_absent_on_disk(
    tmp_path: Path,
) -> None:
    """Issue #187 — when `sut.mode=local`, `sut.autostart=true` and the
    configured `compose_file` does not exist on disk, `run-tests` must
    fail fast with infra exit 2 and `failure_kind="infra"`. Previously
    the runner silently skipped lifecycle and proceeded to the test
    runner, hiding a real misconfiguration."""
    from agentic_os.workflows import run_tests

    conn, paths, events, orch = _runtime(tmp_path)
    try:
        cfg = paths.repo_root / ".qualitycat" / "agentic-os.yml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(_CFG.replace("autostart: false", "autostart: true"), encoding="utf-8")
        # Intentionally do NOT create docker-compose.yml on disk —
        # this mirrors the audit finding in #187. The runner shim must
        # never be invoked: if it is, the test marker file would appear.
        marker = paths.repo_root / "runner-was-invoked.marker"
        for name, body in {
            "run-tests.sh": f"#!/usr/bin/env bash\ntouch '{marker}'\nexit 0\n",
            "scripts/copy-reports.sh": "#!/usr/bin/env bash\nmkdir -p reports\nexit 0\n",
            "scripts/extract-last-run.sh": "#!/usr/bin/env bash\nmkdir -p reports\necho '{\"total\":0,\"passed\":0,\"failed\":0,\"skipped\":0,\"failures\":[],\"discovery_only\":true}' > reports/last-run.json\n",
            "scripts/build-summary.sh": "#!/usr/bin/env bash\nmkdir -p reports\nprintf '# stub\\n' > reports/summary.md\n",
        }.items():
            p = paths.repo_root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
            p.chmod(0o755)

        result = run_tests(orch, paths, events)
        assert result.exit_code == 2, result
        assert result.failure_kind == "infra", result
        assert not marker.exists(), "test runner must NOT execute when compose_file is missing"
        kinds = [e["kind"] for e in events.tail(50)]
        assert "run_tests.sut_lifecycle_failed" in kinds
        # The manifest must surface the specific lifecycle reason so
        # operators can distinguish misconfiguration from `docker
        # compose up` failures.
        assert result.manifest_path
        manifest = json.loads(
            (paths.repo_root / result.manifest_path).read_text(encoding="utf-8")
        )
        lifecycle = manifest.get("sut_lifecycle") or {}
        assert lifecycle.get("ok") is False, manifest
        assert "compose" in (lifecycle.get("error") or "").lower(), manifest
    finally:
        conn.close()


def test_run_tests_aborts_with_infra_exit_when_sut_autostart_compose_missing(
    tmp_path: Path,
) -> None:
    """Issue #108 — when `sut.autostart=true` and the compose file
    exists but `docker compose up` fails, run-tests aborts with infra
    exit 2 instead of falling through to product failure."""
    from agentic_os.workflows import run_tests

    conn, paths, events, orch = _runtime(tmp_path)
    try:
        # Reuse the CFG with autostart turned on and a real compose
        # file path so the lifecycle gate fires.
        cfg = paths.repo_root / ".qualitycat" / "agentic-os.yml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(_CFG.replace("autostart: false", "autostart: true"), encoding="utf-8")
        (paths.repo_root / "docker-compose.yml").write_text(
            "services: {}\n", encoding="utf-8"
        )
        # Runner + reports — runner should never be invoked.
        for name, body in {
            "run-tests.sh": "#!/usr/bin/env bash\necho 'should not run'\nexit 0\n",
            "scripts/copy-reports.sh": "#!/usr/bin/env bash\nmkdir -p reports\nexit 0\n",
            "scripts/extract-last-run.sh": "#!/usr/bin/env bash\nmkdir -p reports\necho '{\"total\":0,\"passed\":0,\"failed\":0,\"skipped\":0,\"failures\":[],\"discovery_only\":true}' > reports/last-run.json\n",
            "scripts/build-summary.sh": "#!/usr/bin/env bash\nmkdir -p reports\nprintf '# stub\\n' > reports/summary.md\n",
        }.items():
            p = paths.repo_root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
            p.chmod(0o755)

        result = run_tests(orch, paths, events)
        # `docker compose up` is not installed in the test environment,
        # so the lifecycle helper returns an infra failure.
        assert result.exit_code == 2
        assert result.failure_kind == "infra"
        kinds = [e["kind"] for e in events.tail(50)]
        assert "run_tests.sut_lifecycle_failed" in kinds
    finally:
        conn.close()
