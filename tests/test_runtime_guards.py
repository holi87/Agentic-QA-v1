from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.runtime.subprocess import run_command
from agentic_os.storage import init_db


OLD_TS = "2000-01-01T00:00:00.000Z"


def _runtime(tmp_path: Path) -> tuple[sqlite3.Connection, Orchestrator]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    return conn, Orchestrator(conn, paths, EventLog(conn, paths))


def test_task_lease_cannot_be_stolen_before_expiry(tmp_path: Path) -> None:
    conn, orch = _runtime(tmp_path)
    try:
        orch.seed_phases()
        task_id = orch.create_task(
            phase_id="04-codex-persistence-guards",
            kind="run",
            payload={"workflow": "unit", "resume_allowed": True},
        )
        orch.lease_task(task_id, owner="worker-a", ttl_seconds=60)

        with pytest.raises(RuntimeError, match="leased by worker-a"):
            orch.lease_task(task_id, owner="worker-b", ttl_seconds=60)
    finally:
        conn.close()


def test_recovery_requeues_expired_leased_task_and_is_idempotent(tmp_path: Path) -> None:
    conn, orch = _runtime(tmp_path)
    try:
        orch.seed_phases()
        task_id = orch.create_task(
            phase_id="04-codex-persistence-guards",
            kind="run",
            payload={"workflow": "unit", "resume_allowed": True},
        )
        orch.lease_task(task_id, owner="lost-worker", ttl_seconds=60)
        conn.execute("UPDATE tasks SET lease_expires=? WHERE id=?;", (OLD_TS, task_id))

        first = orch.recovery_scan()
        second = orch.recovery_scan()

        row = conn.execute("SELECT status, lease_owner, lease_expires FROM tasks WHERE id=?;", (task_id,)).fetchone()
        assert row["status"] == "queued"
        assert row["lease_owner"] is None
        assert row["lease_expires"] is None
        assert first["expired_task_leases"] == [task_id]
        assert second["expired_task_leases"] == []
    finally:
        conn.close()


def test_recovery_marks_expired_running_task_abandoned_once(tmp_path: Path) -> None:
    conn, orch = _runtime(tmp_path)
    try:
        orch.seed_phases()
        task_id = orch.create_task(
            phase_id="04-codex-persistence-guards",
            kind="run",
            payload={"workflow": "unit", "resume_allowed": True},
        )
        orch.lease_task(task_id, owner="lost-worker", ttl_seconds=60)
        orch.mark_running(task_id, owner="lost-worker")
        conn.execute("UPDATE tasks SET lease_expires=? WHERE id=?;", (OLD_TS, task_id))

        first = orch.recovery_scan()
        second = orch.recovery_scan()

        row = conn.execute("SELECT status, error_class, lease_owner FROM tasks WHERE id=?;", (task_id,)).fetchone()
        assert row["status"] == "failed"
        assert row["error_class"] == "abandoned"
        assert row["lease_owner"] is None
        assert first["abandoned_tasks"] == [task_id]
        assert second["abandoned_tasks"] == []
    finally:
        conn.close()


def test_safe_subprocess_writes_stdout_stderr_and_status(tmp_path: Path) -> None:
    log_path = tmp_path / "subprocess.log"
    result = run_command(
        [
            sys.executable,
            "-c",
            "import sys; print('hello stdout'); print('hello stderr', file=sys.stderr)",
        ],
        cwd=tmp_path,
        log_path=log_path,
        timeout_seconds=5,
    )

    log = log_path.read_text(encoding="utf-8")
    assert result.exit_code == 0
    assert "[stdout] hello stdout" in log
    assert "[stderr] hello stderr" in log
    assert '"status": "finished"' in log
    assert '"exit_code": 0' in log


def test_safe_subprocess_timeout_maps_to_infra_exit_and_status(tmp_path: Path) -> None:
    log_path = tmp_path / "timeout.log"
    result = run_command(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        log_path=log_path,
        timeout_seconds=1,
        shutdown_grace_seconds=1,
    )

    log = log_path.read_text(encoding="utf-8")
    assert result.exit_code == 2
    assert result.failure_kind == "timeout"
    assert result.timed_out is True
    assert '"timed_out": true' in log
