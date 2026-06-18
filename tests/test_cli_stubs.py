"""Tests for the previously-stubbed CLI surfaces (issue #139).

Four features land in one PR and share a fixture set:

- ``up --daemon`` — pidfile + log file plumbing, refuses on a live
  pidfile, cleans stale ones.
- ``down`` — reads pidfile, sends SIGTERM then SIGKILL, removes the
  pidfile.
- ``logs --follow`` — tails the dashboard log; errors when missing.
- ``init --install-shim`` — drops a portable wrapper into
  ``~/.local/bin`` (overridable via ``--shim-dir``).

The ``up --daemon`` tests monkey-patch ``subprocess.Popen`` so the
suite never actually launches a dashboard. ``down`` tests use real
short-lived subprocesses to exercise the signal path end-to-end.
"""
from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List

import pytest

import agentic_os.cli as cli
from agentic_os.cli import (
    _install_agentic_os_shim,
    _process_alive,
    cmd_down,
    cmd_init,
    cmd_logs,
    cmd_up,
)
from agentic_os.errors import InfraError, UsageError
from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db


def _capture(func, *args, json_output: bool = True, **kwargs) -> tuple[int, str]:
    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf
    try:
        rc = func(*args, json_output=json_output, **kwargs)
    finally:
        sys.stdout = real_stdout
    return rc, buf.getvalue()


def _seed_runtime(repo: Path) -> RuntimePaths:
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / "agentic-os-runtime")
    paths.ensure()
    cfg = repo / "config" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        """
runtime:
  root: agentic-os-runtime
  timezone: Europe/Warsaw
  max_parallel_tasks: 1
  heartbeat_seconds: 20
  lease_ttl_seconds: 60
  stale_lease_seconds: 90
  shutdown_grace_seconds: 5
  timeouts:
    default_seconds: 1800
    docker_seconds: 240
    test_seconds: 3600
    model_seconds: 1800
    report_seconds: 300
sut:
  root: .
  compose_file: docker-compose.yml
  compose_project_name: agentic-os-sut
  autostart: false
  healthcheck:
    command: ["true"]
    timeout_seconds: 30
    retries: 1
  test_runner: ./run-tests.sh
  install_shim_allowed: false
models:
  planner: { provider: claude, command: ["claude"], role: opus }
  implementer: { provider: claude, command: ["claude"], role: sonnet }
  reviewer: { provider: codex, command: ["codex"], role: codex }
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
""".lstrip(),
        encoding="utf-8",
    )
    conn = init_db(paths.db)
    Orchestrator(conn, paths, EventLog(conn, paths)).seed_phases()
    conn.close()
    return paths


# ---------------------------------------------------------------------------
# up --daemon
# ---------------------------------------------------------------------------


class _FakePopen:
    def __init__(self, *args, **kwargs):  # noqa: D401
        # Mirror the bits of subprocess.Popen the daemon helper relies on.
        # The real os pid is fine — it stays alive (we're it).
        self.pid = os.getpid()
        self.args = args
        self.kwargs = kwargs


def test_up_daemon_writes_pidfile_and_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = _seed_runtime(repo)
    monkeypatch.setattr(cli.subprocess, "Popen", _FakePopen) if hasattr(cli, "subprocess") else None
    # The helper imports subprocess inside the function — patch the
    # canonical module instead.
    monkeypatch.setattr("subprocess.Popen", _FakePopen)

    rc, out = _capture(
        cmd_up,
        repo,
        ["--dashboard-only", "--daemon"],
        json_output=True,
        config_override=None,
    )
    assert rc == 0
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["pid"] == os.getpid()
    pid_path = paths.pids_dir / "dashboard.pid"
    assert pid_path.exists()
    assert pid_path.read_text(encoding="utf-8").strip() == str(os.getpid())
    log_path = paths.logs_dir / "dashboard.log"
    assert log_path.exists()


def test_up_daemon_refuses_when_pidfile_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = _seed_runtime(repo)
    # Use our own pid — guaranteed alive.
    (paths.pids_dir / "dashboard.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    with pytest.raises(UsageError, match="already running"):
        cmd_up(repo, ["--dashboard-only", "--daemon"], json_output=True, config_override=None)


def test_up_daemon_clears_stale_pidfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = _seed_runtime(repo)
    # Spawn-and-reap to grab a definitely-dead pid.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead_pid = proc.pid
    assert not _process_alive(dead_pid)
    (paths.pids_dir / "dashboard.pid").write_text(f"{dead_pid}\n", encoding="utf-8")

    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    rc, _ = _capture(
        cmd_up,
        repo,
        ["--dashboard-only", "--daemon"],
        json_output=True,
        config_override=None,
    )
    assert rc == 0
    # The stale pid is gone; the new pid (ours) is written.
    assert (paths.pids_dir / "dashboard.pid").read_text(encoding="utf-8").strip() == str(os.getpid())


def test_up_daemon_rejects_foreground_combo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_runtime(repo)
    with pytest.raises(UsageError, match="incompatible"):
        cmd_up(
            repo,
            ["--dashboard-only", "--daemon", "--foreground"],
            json_output=True,
            config_override=None,
        )


# ---------------------------------------------------------------------------
# down
# ---------------------------------------------------------------------------


def test_down_no_pidfile(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_runtime(repo)
    rc, out = _capture(cmd_down, repo, [], json_output=True)
    assert rc == 0
    payload = json.loads(out)
    assert payload["reason"] == "no_pidfile"


def test_down_clears_stale_pidfile(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = _seed_runtime(repo)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead_pid = proc.pid
    pid_path = paths.pids_dir / "dashboard.pid"
    pid_path.write_text(f"{dead_pid}\n", encoding="utf-8")

    rc, out = _capture(cmd_down, repo, [], json_output=True)
    assert rc == 0
    payload = json.loads(out)
    assert payload["reason"] == "stale_pidfile"
    assert not pid_path.exists()


def test_down_terminates_running_subprocess(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = _seed_runtime(repo)
    # Spawn a real subprocess that sleeps until signalled.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        pid_path = paths.pids_dir / "dashboard.pid"
        pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")
        assert _process_alive(proc.pid)

        rc, out = _capture(cmd_down, repo, ["--timeout", "2"], json_output=True)
        assert rc == 0
        payload = json.loads(out)
        assert payload["pid"] == proc.pid
        assert not pid_path.exists()
        proc.wait(timeout=5)
        assert not _process_alive(proc.pid)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_down_escalates_to_sigkill_when_sigterm_ignored(tmp_path: Path) -> None:
    """If SIGTERM is swallowed, down must escalate to SIGKILL within
    the configured timeout. We simulate the swallow with a child that
    ignores SIGTERM via ``signal.signal(SIGTERM, SIG_IGN)``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = _seed_runtime(repo)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)",
        ]
    )
    try:
        pid_path = paths.pids_dir / "dashboard.pid"
        pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")
        rc, out = _capture(cmd_down, repo, ["--timeout", "0.3"], json_output=True)
        assert rc == 0
        payload = json.loads(out)
        assert payload["escalated_to_sigkill"] is True
        assert not pid_path.exists()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------------------
# logs --follow
# ---------------------------------------------------------------------------


def test_logs_follow_errors_when_log_missing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_runtime(repo)
    with pytest.raises(InfraError, match="missing"):
        cmd_logs(repo, ["--follow"], json_output=False)


def test_logs_follow_streams_then_exits_on_sigint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = _seed_runtime(repo)
    log_path = paths.logs_dir / "dashboard.log"
    log_path.write_text("line-1\nline-2\nline-3\n", encoding="utf-8")

    # Raise KeyboardInterrupt the first time the follow loop sleeps —
    # mirrors what Ctrl+C does in interactive use without us needing to
    # actually send a signal across threads.
    def _interrupt_sleep(_: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", _interrupt_sleep)
    rc, out = _capture(cmd_logs, repo, ["--follow", "--lines", "2"], json_output=False)
    assert rc == 0
    # `--lines 2` should yield the last two lines before --follow blocks.
    assert "line-2" in out
    assert "line-3" in out


# ---------------------------------------------------------------------------
# init --install-shim
# ---------------------------------------------------------------------------


def test_install_shim_writes_executable_wrapper(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parent.parent  # actual checkout
    shim_dir = tmp_path / "bin"
    info = _install_agentic_os_shim(repo, shim_dir=shim_dir, force=False)
    shim = Path(info["shim_path"])
    assert shim.exists()
    mode = shim.stat().st_mode & 0o777
    assert mode & 0o100, f"shim should be executable, mode={oct(mode)}"
    contents = shim.read_text(encoding="utf-8")
    assert contents.startswith("#!/usr/bin/env bash")
    assert "agentic_os" in contents
    assert "PYTHONPATH" in contents


def test_install_shim_refuses_without_force(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parent.parent
    shim_dir = tmp_path / "bin"
    _install_agentic_os_shim(repo, shim_dir=shim_dir, force=False)
    with pytest.raises(UsageError, match="already exists"):
        _install_agentic_os_shim(repo, shim_dir=shim_dir, force=False)


def test_install_shim_overwrites_with_force(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parent.parent
    shim_dir = tmp_path / "bin"
    info = _install_agentic_os_shim(repo, shim_dir=shim_dir, force=False)
    Path(info["shim_path"]).write_text("# stale\n", encoding="utf-8")
    second = _install_agentic_os_shim(repo, shim_dir=shim_dir, force=True)
    assert second["shim_path"] == info["shim_path"]
    contents = Path(second["shim_path"]).read_text(encoding="utf-8")
    assert "stale" not in contents


def test_install_shim_reports_path_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = Path(__file__).resolve().parent.parent
    shim_dir = tmp_path / "elsewhere"
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # known not to include tmp_path
    info = _install_agentic_os_shim(repo, shim_dir=shim_dir, force=False)
    assert info["on_path"] is False

    monkeypatch.setenv("PATH", f"{shim_dir}:/usr/bin")
    info2 = _install_agentic_os_shim(repo, shim_dir=shim_dir, force=True)
    assert info2["on_path"] is True


def test_install_shim_refuses_when_package_sources_missing(tmp_path: Path) -> None:
    """`_install_agentic_os_shim` must refuse if pointed at a directory
    that is not actually a checkout — otherwise the generated wrapper
    would point at a non-existent ``scripts/agentic-os/agentic_os/``
    package and silently fail at first invocation.
    """
    fake_repo = tmp_path / "not-a-checkout"
    fake_repo.mkdir()
    shim_dir = tmp_path / "bin"
    with pytest.raises(InfraError, match="cannot find"):
        _install_agentic_os_shim(fake_repo, shim_dir=shim_dir, force=False)
