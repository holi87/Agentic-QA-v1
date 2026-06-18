"""Model role schema, prompt delivery, and final-gate manifest runtime contracts."""
from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path


def test_model_invocations_schema_accepts_antigravity_provider(tmp_path: Path) -> None:
    """Issue #102 — schema CHECK constraint must accept the default
    triager (provider=antigravity, role=gemini)."""
    from agentic_os.events import EventLog
    from agentic_os.paths import RuntimePaths
    from agentic_os.storage import init_db
    from agentic_os.ids import ulid
    from agentic_os.time_utils import now_iso

    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    try:
        conn.execute(
            """
            INSERT INTO model_invocations(
                id, model_role, provider, command, started_at
            ) VALUES (?, ?, ?, ?, ?);
            """,
            (ulid(), "gemini", "antigravity", '["gemini"]', now_iso()),
        )
        # No CHECK error means the migration applied.
        rows = list(conn.execute("SELECT provider, model_role FROM model_invocations;"))
        assert any(r["provider"] == "antigravity" for r in rows)
    finally:
        conn.close()


def test_run_command_streams_input_text_to_stdin(tmp_path: Path) -> None:
    """Issue #102 — `run_command(input_text=...)` must deliver the
    payload via stdin so a model CLI can read the prompt."""
    from agentic_os.runtime.subprocess import run_command

    log_path = tmp_path / "run.log"
    res = run_command(
        ["/bin/cat"],
        cwd=tmp_path,
        log_path=log_path,
        timeout_seconds=5,
        input_text="hello-from-prompt",
    )
    assert res.exit_code == 0
    log = log_path.read_text(encoding="utf-8")
    assert "hello-from-prompt" in log


def test_final_gate_manifest_lists_configured_model_roles(tmp_path: Path) -> None:
    """Issue #101 — manifest carries a `models_roles` block listing
    every configured role + whether the binary is reachable, so
    operators can see which automation is wired vs. only declared."""
    from agentic_os.events import EventLog
    from agentic_os.orchestrator import Orchestrator
    from agentic_os.paths import RuntimePaths
    from agentic_os.storage import init_db
    from agentic_os.workflows import run_final_gate

    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()

    cfg = repo / "config" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        textwrap.dedent(
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
              triager: {provider: antigravity, command: ["gemini"], role: gemini, auto_fire: true}
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
        ),
        encoding="utf-8",
    )

    result = run_final_gate(orch, paths, events)
    manifest = json.loads((paths.repo_root / result.manifest_path).read_text(encoding="utf-8"))
    roles = manifest["models_roles"]
    assert set(roles.keys()) == {"planner", "implementer", "reviewer", "triager"}
    assert roles["triager"]["provider"] == "antigravity"
    assert roles["triager"]["role"] == "gemini"
    assert roles["triager"]["auto_fire"] is True
    # Binary checks honestly reflect the test environment.
    assert "binary_on_path" in roles["planner"]
    conn.close()
