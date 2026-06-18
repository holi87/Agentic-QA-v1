from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_os import autonomy
from agentic_os.autonomy import (
    _exploratory_pass,
    _SessionState,
    current_status,
    preflight_check,
    start_session,
    stop_session,
)
from agentic_os.paths import RuntimePaths


def _write_config(repo: Path, *, sut_root: str = ".", test_runner: str = "./run-tests.sh") -> None:
    cfg = repo / "config" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        f"""
runtime:
  root: .agentic-os
  timezone: Europe/Warsaw
  max_parallel_tasks: 4
  heartbeat_seconds: 20
  lease_ttl_seconds: 60
  stale_lease_seconds: 90
  shutdown_grace_seconds: 5
  timeouts:
    default_seconds: 30
    docker_seconds: 30
    test_seconds: 30
    model_seconds: 30
    report_seconds: 30
sut:
  root: {sut_root}
  compose_file: docker-compose.yml
  compose_project_name: agentic-os-sut
  autostart: false
  healthcheck:
    command: ["true"]
    timeout_seconds: 1
    retries: 0
  test_runner: {test_runner}
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
  prompts: config/prompts
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
""".lstrip(),
        encoding="utf-8",
    )


def _write_online_config(repo: Path, *, web_url: str = "https://qualitycat.com.pl") -> None:
    cfg = repo / "config" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        f"""
runtime:
  root: .agentic-os
  timezone: Europe/Warsaw
  max_parallel_tasks: 4
  heartbeat_seconds: 20
  lease_ttl_seconds: 60
  stale_lease_seconds: 90
  shutdown_grace_seconds: 5
  timeouts:
    default_seconds: 30
    docker_seconds: 30
    test_seconds: 30
    model_seconds: 30
    report_seconds: 30
sut:
  root: .
  mode: online
  compose_file: null
  compose_project_name: online-sut
  autostart: false
  healthcheck:
    command: ["true"]
    timeout_seconds: 1
    retries: 0
  test_runner: ./run-tests.sh
  install_shim_allowed: false
  web:
    enabled: true
    url: {web_url}
  api:
    enabled: false
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
  prompts: config/prompts
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
""".lstrip(),
        encoding="utf-8",
    )


def _make_paths(tmp_path: Path) -> RuntimePaths:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    return paths


@pytest.fixture(autouse=True)
def _reset_autonomy_singletons():
    """Force a fresh session before/after each test."""
    autonomy._SESSION = None
    autonomy._STOP_EVENT = None
    autonomy._THREAD = None
    yield
    if autonomy._STOP_EVENT is not None:
        autonomy._STOP_EVENT.set()
    autonomy._SESSION = None
    autonomy._STOP_EVENT = None
    autonomy._THREAD = None


def test_preflight_flags_stack_unknown_for_empty_sut_root(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    _write_config(paths.repo_root)
    # Repo root has no package.json / pyproject.toml — discovery returns
    # stack=unknown and markers=0, which preflight must surface as `fail`.
    result = preflight_check(paths)
    assert result["ok"] is False
    stack_check = next(c for c in result["checks"] if c["id"] == "sut_stack")
    assert stack_check["status"] == "fail"
    assert "stack=unknown" in stack_check["message"]
    assert stack_check["actions"], "actionable hints are mandatory on fail"


def test_preflight_passes_for_python_sut(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    (paths.repo_root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    runner = paths.repo_root / "run-tests.sh"
    runner.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    runner.chmod(0o755)
    _write_config(paths.repo_root)
    result = preflight_check(paths)
    assert result["ok"] is True
    statuses = {c["id"]: c["status"] for c in result["checks"]}
    assert statuses["sut_stack"] == "pass"
    assert statuses["test_runner"] == "pass"
    assert statuses["config"] == "pass"


def test_preflight_uses_online_web_url_without_local_stack(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    runner = paths.repo_root / "run-tests.sh"
    runner.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    runner.chmod(0o755)
    _write_online_config(paths.repo_root)
    result = preflight_check(paths)
    assert result["ok"] is True
    ids = {c["id"] for c in result["checks"]}
    assert "sut_online_web" in ids
    assert "sut_stack" not in ids
    web_check = next(c for c in result["checks"] if c["id"] == "sut_online_web")
    assert "https://qualitycat.com.pl" in web_check["message"]


def test_preflight_warns_when_test_runner_missing(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    (paths.repo_root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    _write_config(paths.repo_root)  # references ./run-tests.sh that does not exist
    result = preflight_check(paths)
    runner_check = next(c for c in result["checks"] if c["id"] == "test_runner")
    assert runner_check["status"] == "warn"
    assert "does not exist" in runner_check["message"]


def test_exploratory_pass_returns_stack_unknown_signal(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    _write_config(paths.repo_root)
    session = _SessionState(
        session_id="t",
        started_at="2026-05-20T00:00:00Z",
        expected_finish_at="2026-05-20T01:00:00Z",
        max_minutes=60,
    )
    signal = _exploratory_pass(session, paths)
    assert signal == "stack_unknown"
    assert any(ev["step"] == "exploratory" for ev in session.events_log)


def test_exploratory_pass_returns_none_for_node_sut(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    (paths.repo_root / "package.json").write_text("{}", encoding="utf-8")
    _write_config(paths.repo_root)
    session = _SessionState(
        session_id="t",
        started_at="2026-05-20T00:00:00Z",
        expected_finish_at="2026-05-20T01:00:00Z",
        max_minutes=60,
    )
    signal = _exploratory_pass(session, paths)
    assert signal is None


def test_exploratory_pass_uses_online_web_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _make_paths(tmp_path)
    _write_online_config(paths.repo_root)
    seen = {}

    def fake_crawl(start_url: str, **_kwargs):
        seen["start_url"] = start_url
        return SimpleNamespace(pages=[
            SimpleNamespace(url=start_url),
            SimpleNamespace(url=start_url.rstrip("/") + "/kontakt"),
        ])

    import agentic_os.crawler as crawler

    monkeypatch.setattr(crawler, "crawl_same_origin", fake_crawl)
    session = _SessionState(
        session_id="t",
        started_at="2026-05-20T00:00:00Z",
        expected_finish_at="2026-05-20T01:00:00Z",
        max_minutes=60,
    )
    signal = _exploratory_pass(session, paths)
    # Issue #317 — online-only defaults the exploratory baseline on, so the
    # empty-queue online probe is a benign idle (None), not a block. It still
    # crawls the saved web URL and records the source.
    assert signal is None
    assert seen["start_url"] == "https://qualitycat.com.pl"
    online_event = next(ev for ev in session.events_log if ev["step"] == "exploratory:online")
    assert "source=sut.web.url" in online_event["detail"]


def test_start_session_attaches_preflight_payload(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    _write_config(paths.repo_root)
    session = start_session(paths, max_minutes=15)
    try:
        # Race-safe: preflight is set before the thread starts running.
        assert session.preflight is not None
        assert session.preflight["ok"] is False
        ids = {c["id"] for c in session.preflight["checks"]}
        assert {"config", "sut_root", "sut_stack"}.issubset(ids)
        status = current_status()
        assert status["session"]["preflight"] is not None
    finally:
        stop_session()


def test_empty_queue_online_session_runs_exploratory_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #317 — an online-only SUT with an empty queue must turn idle time
    into exploratory tests, not park on the deferred-synthesis block. The
    baseline is default-on for online mode (no flag editing required)."""
    paths = _make_paths(tmp_path)
    runner = paths.repo_root / "run-tests.sh"
    runner.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    runner.chmod(0o755)
    _write_online_config(paths.repo_root)

    import agentic_os.crawler as crawler

    monkeypatch.setattr(
        crawler,
        "crawl_same_origin",
        lambda start_url, **_kwargs: SimpleNamespace(pages=[SimpleNamespace(url=start_url)]),
    )
    monkeypatch.setattr(autonomy, "_ACTIVE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(autonomy, "_PAUSED_POLL_SECONDS", 0.05)
    session = start_session(paths, max_minutes=15)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            steps = [ev["step"] for ev in current_status()["session"]["events_log"]]
            if "exploratory:baseline" in steps:
                break
            time.sleep(0.05)
        status = current_status()
        steps = [ev["step"] for ev in status["session"]["events_log"]]
        assert "exploratory:baseline" in steps
        assert "idle:blocked" not in steps
        assert not status["session"].get("paused_reason")
        assert status["session"]["status"] == "running"
        assert list((paths.repo_root / "reports").glob("exploratory-baseline-*.json"))
    finally:
        stop_session()


def test_loop_guard_pauses_after_threshold_stack_unknown_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force a tight loop and confirm the session pauses + exposes a reason."""
    paths = _make_paths(tmp_path)
    _write_config(paths.repo_root)
    monkeypatch.setattr(autonomy, "_EXPLORATORY_FAILURE_THRESHOLD", 2)
    monkeypatch.setattr(autonomy, "_ACTIVE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(autonomy, "_PAUSED_POLL_SECONDS", 0.05)
    session = start_session(paths, max_minutes=15)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = current_status()
            if status["session"].get("paused_reason"):
                break
            time.sleep(0.05)
        status = current_status()
        assert status["session"]["paused_reason"], "expected paused_reason after threshold"
        assert "stack=unknown" in status["session"]["paused_reason"]
        # The session must still be "running" — pause is in-loop, not a hard stop.
        assert status["session"]["status"] == "running"
        # And the timeline must include a single `idle:paused` event, not a
        # spam of `idle:awaiting-task`.
        steps = [ev["step"] for ev in status["session"]["events_log"]]
        assert "idle:paused" in steps
    finally:
        stop_session()


def test_loop_guard_resumes_when_config_is_fixed_while_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paused session must auto-recover when discovery becomes healthy."""
    paths = _make_paths(tmp_path)
    _write_config(paths.repo_root)
    monkeypatch.setattr(autonomy, "_EXPLORATORY_FAILURE_THRESHOLD", 2)
    monkeypatch.setattr(autonomy, "_ACTIVE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(autonomy, "_PAUSED_POLL_SECONDS", 0.05)
    session = start_session(paths, max_minutes=15)
    try:
        # Wait for the pause to engage.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if current_status()["session"].get("paused_reason"):
                break
            time.sleep(0.05)
        assert current_status()["session"]["paused_reason"], "expected pause before fix"

        # Operator fix: drop a stack marker while the loop is paused. The
        # next exploratory probe must detect it and clear the pause.
        (paths.repo_root / "pyproject.toml").write_text(
            "[project]\nname='x'\n", encoding="utf-8"
        )

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = current_status()
            if status["session"].get("paused_reason") is None:
                steps = [ev["step"] for ev in status["session"]["events_log"]]
                if "idle:resumed" in steps:
                    break
            time.sleep(0.05)
        status = current_status()
        assert status["session"]["paused_reason"] is None
        steps = [ev["step"] for ev in status["session"]["events_log"]]
        assert "idle:resumed" in steps
    finally:
        stop_session()
