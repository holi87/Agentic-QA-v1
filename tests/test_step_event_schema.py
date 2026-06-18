"""Step event taxonomy schema, throttling, SSE filtering, and model invocation step events."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from agentic_os.events import EventLog, StepSchemaError
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db


def _events(tmp_path: Path, throttle: int = 5) -> tuple[EventLog, RuntimePaths]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    return EventLog(conn, paths, step_progress_throttle=throttle), paths


def test_start_end_step_emit_validated_payloads(tmp_path: Path) -> None:
    events, _paths = _events(tmp_path)
    sid = events.start_step(
        kind="planner",
        phase="analyze",
        actor="claude-autopilot",
        role="planner",
        provider="claude",
        detail="plan slice",
    )
    events.end_step(
        sid,
        outcome="ok",
        kind="planner",
        phase="analyze",
        actor="claude-autopilot",
        role="planner",
        provider="claude",
        detail="done",
    )
    tail = events.tail(10)
    kinds = [e["kind"] for e in tail]
    assert "step.start" in kinds and "step.end" in kinds
    start_payload = next(e["payload"] for e in tail if e["kind"] == "step.start")
    end_payload = next(e["payload"] for e in tail if e["kind"] == "step.end")
    assert start_payload["step_id"] == sid
    assert start_payload["outcome"] is None and start_payload["ended_at"] is None
    assert end_payload["step_id"] == sid
    assert end_payload["outcome"] == "ok"
    assert end_payload["ended_at"] is not None


def test_invalid_step_kind_rejected(tmp_path: Path) -> None:
    events, _paths = _events(tmp_path)
    with pytest.raises(StepSchemaError, match="step.kind invalid"):
        events.start_step(
            kind="not-a-kind",
            phase="analyze",
            actor="orchestrator",
        )


def test_invalid_step_phase_rejected(tmp_path: Path) -> None:
    events, _paths = _events(tmp_path)
    with pytest.raises(StepSchemaError, match="step.phase invalid"):
        events.start_step(kind="planner", phase="weird", actor="orchestrator")


def test_end_step_requires_known_outcome(tmp_path: Path) -> None:
    events, _paths = _events(tmp_path)
    sid = events.start_step(kind="gate", phase="gate", actor="orchestrator")
    with pytest.raises(StepSchemaError, match="step.outcome invalid"):
        events.end_step(
            sid,
            outcome="weird",
            kind="gate",
            phase="gate",
            actor="orchestrator",
        )


def test_progress_step_respects_throttle(tmp_path: Path) -> None:
    events, _paths = _events(tmp_path, throttle=3)
    sid = events.start_step(kind="run-tests", phase="run", actor="orchestrator")
    emitted = [events.progress_step(sid, f"line {i}") for i in range(10)]
    accepted = [e for e in emitted if e is not None]
    assert len(accepted) == 3
    # After the 1s window resets, more progress is allowed.
    time.sleep(1.05)
    extra = events.progress_step(sid, "next window")
    assert extra is not None


def test_progress_step_drops_when_throttle_zero(tmp_path: Path) -> None:
    events, _paths = _events(tmp_path, throttle=0)
    sid = events.start_step(kind="generator", phase="generate", actor="orchestrator")
    assert events.progress_step(sid, "anything") is None


def test_invoke_model_emits_step_start_and_end(tmp_path: Path) -> None:
    """Issue #245 — model invocations are wrapped in step.start / step.end."""
    import os
    from agentic_os.models import invoke_model

    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    fake = tmp_path / "bin" / "fake-script"
    fake.parent.mkdir(parents=True, exist_ok=True)
    version = "fake-script 1.0"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f"if [ \"${{1:-}}\" = \"--version\" ]; then echo '{version}'; exit 0; fi\n"
        "cat >/dev/null\n"
        "printf '%s\\n' '{\"envelope\":{\"schema_version\":\"1.0\",\"provider\":\"script\","
        "\"provider_version\":\"fake-script 1.0\",\"role\":\"planner\",\"verdict\":null,"
        "\"reason\":null,\"citations\":[],\"body\":\"ok\",\"metadata\":{}}}'\n",
        encoding="utf-8",
    )
    os.chmod(fake, 0o755)
    invoke_model(
        conn,
        paths,
        events,
        role="planner",
        config={"planner": {"provider": "script", "command": [str(fake)], "role": "script"}},
        prompt="plan",
        timeout_seconds=5,
    )
    tail = events.tail(20)
    kinds = [e["kind"] for e in tail]
    assert "step.start" in kinds and "step.end" in kinds
    end_payload = next(e["payload"] for e in tail if e["kind"] == "step.end")
    assert end_payload["kind"] == "planner"
    assert end_payload["phase"] == "analyze"
    assert end_payload["provider"] == "script"
    assert end_payload["outcome"] == "ok"
    assert end_payload["log_ref"]


def test_invoke_model_terminates_step_when_subprocess_raises(tmp_path: Path) -> None:
    """Codex PR #276 review (P1) — step must end with failed even on exception."""
    import os
    from unittest.mock import patch

    from agentic_os.models import invoke_model

    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    fake = tmp_path / "bin" / "fake-script"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"${1:-}\" = \"--version\" ]; then echo 'v1'; exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    os.chmod(fake, 0o755)
    # Force run_command to raise mid-attempt; the step.start must still
    # produce a matching step.end with outcome="failed".
    with patch(
        "agentic_os.models.run_command",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(RuntimeError):
            invoke_model(
                conn,
                paths,
                events,
                role="planner",
                config={"planner": {"provider": "script", "command": [str(fake)], "role": "script"}},
                prompt="plan",
                timeout_seconds=5,
            )
    tail = events.tail(20)
    starts = [e for e in tail if e["kind"] == "step.start"]
    ends = [e for e in tail if e["kind"] == "step.end"]
    assert len(starts) == 1 and len(ends) == 1
    assert ends[0]["payload"]["outcome"] == "failed"
    assert ends[0]["payload"]["step_id"] == starts[0]["payload"]["step_id"]


def test_event_log_for_paths_reads_config_throttle(tmp_path: Path) -> None:
    """Codex PR #276 review (P2) — factory loads the real config file."""
    import textwrap

    from agentic_os.events import event_log_for_paths

    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    cfg_path = repo / "config" / "agentic-os.yml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(_BASIC_CONFIG.replace("{throttle}", "0"), encoding="utf-8")
    conn = init_db(paths.db)
    events = event_log_for_paths(conn, paths)
    assert events.step_progress_throttle == 0


_BASIC_CONFIG = """\
runtime:
  root: agentic-os-runtime
  timezone: Europe/Warsaw
  max_parallel_tasks: 1
  heartbeat_seconds: 10
  lease_ttl_seconds: 600
  stale_lease_seconds: 1800
  shutdown_grace_seconds: 30
  timeouts:
    default_seconds: 600
    docker_seconds: 120
    test_seconds: 900
    model_seconds: 600
    report_seconds: 120

sut:
  root: .
  compose_file: docker-compose.yml
  compose_project_name: app
  autostart: false
  healthcheck:
    command: ["sh", "-c", "exit 0"]
    timeout_seconds: 5
    retries: 1
  test_runner: scripts/run-tests.sh
  install_shim_allowed: false

models:
  planner:
    provider: claude
    command: ["claude", "--model", "opus"]
    role: opus
  implementer:
    provider: claude
    command: ["claude", "--model", "sonnet"]
    role: sonnet
  reviewer:
    provider: codex
    command: ["codex"]
    role: codex

dashboard:
  host: 127.0.0.1
  port: 8765
  enable_write_endpoints: false

paths:
  reports: reports
  bugs: bugs
  evidence: evidence
  prompts: prompts

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

events:
  step_progress_throttle: {throttle}
"""


def test_event_log_from_config_reads_throttle(tmp_path: Path) -> None:
    from agentic_os.events import event_log_from_config

    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    log = event_log_from_config(conn, paths, {"events": {"step_progress_throttle": 2}})
    assert log.step_progress_throttle == 2
    default_log = event_log_from_config(conn, paths, {})
    assert default_log.step_progress_throttle == 5


def test_sse_kind_filter_glob_and_exact() -> None:
    from agentic_os.routes.dashboard_server import _parse_kind_filter

    assert _parse_kind_filter("/api/events") is None
    glob = _parse_kind_filter("/api/events?kind=step.*")
    assert glob is not None
    assert glob("step.start") and glob("step.end") and glob("step.progress")
    assert not glob("model.invoked")
    exact = _parse_kind_filter("/api/events?kind=step.start,sut.git.init")
    assert exact("step.start") and exact("sut.git.init")
    assert not exact("step.end")


def test_step_end_drops_throttle_bucket(tmp_path: Path) -> None:
    events, _paths = _events(tmp_path, throttle=2)
    sid = events.start_step(kind="git", phase="run", actor="orchestrator")
    events.progress_step(sid, "a")
    events.end_step(
        sid,
        outcome="ok",
        kind="git",
        phase="run",
        actor="orchestrator",
    )
    # Internal bucket should be cleared once the step terminates.
    assert sid not in events._progress_buckets  # type: ignore[attr-defined]
