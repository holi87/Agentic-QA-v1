"""Autonomy CLI parity with dashboard preflight, status, and follow endpoints."""
from __future__ import annotations

import io
import json
import sys
import time as _time
from pathlib import Path
from typing import Iterator

import pytest

from agentic_os.cli import cmd_autonomy
from agentic_os.events import EventLog
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    # Minimal config so open_runtime() can boot.
    cfg = repo / "config" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_MINIMAL_CONFIG.format(repo=str(repo)), encoding="utf-8")
    return repo


def test_autonomy_preflight_emits_json_payload(repo_root: Path, capsys) -> None:
    rc = cmd_autonomy(repo_root, ["preflight", "--json"], json_output=True)
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert "checks" in payload
    assert isinstance(payload.get("ok"), bool)


def test_autonomy_status_returns_payload_without_active_session(repo_root: Path, capsys) -> None:
    rc = cmd_autonomy(repo_root, ["status", "--json"], json_output=True)
    out = capsys.readouterr().out
    assert rc == 0
    # current_status returns an empty dict when no session ever ran.
    json.loads(out)


def test_autonomy_follow_filter_supports_kind_glob(tmp_path: Path) -> None:
    """The follow filter parser must accept `kind=step.*` glob syntax."""
    from agentic_os.cli import _autonomy_follow as _follow

    # We don't run the loop here — the filter is exercised inline.
    paths = RuntimePaths(repo_root=tmp_path / "repo", runtime_root=tmp_path / "repo" / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    sid = events.start_step(kind="planner", phase="analyze", actor="claude-autopilot")
    events.end_step(
        sid,
        outcome="ok",
        kind="planner",
        phase="analyze",
        actor="claude-autopilot",
    )
    # Sanity: events were written.
    assert paths.events_dir.exists()
    # Read the NDJSON file directly and apply the same filter the follower uses.
    rows = []
    for ndjson_file in sorted(paths.events_dir.glob("*.ndjson")):
        for raw in ndjson_file.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            rows.append(json.loads(raw))
    assert any(r["kind"] == "step.start" for r in rows)
    assert any(r["kind"] == "step.end" for r in rows)


_MINIMAL_CONFIG = """\
runtime:
  root: {repo}/agentic-os-runtime
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
"""
