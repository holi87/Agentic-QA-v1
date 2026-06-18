"""Issue #274 — `doctor --repair` stale-lease repair, dry-run safety.

A ``leases`` row whose owning process is dead must be cleared by
``doctor --repair --yes`` WITHOUT any operator input (safe in non-tty /
autonomous contexts), while a dry-run must NOT mutate the table.
"""
from __future__ import annotations

import socket
from pathlib import Path

from agentic_os import repair
from agentic_os.events import EventLog
from agentic_os.paths import runtime_paths
from agentic_os.storage.db import init_db

# A pid that is essentially never a live process on this host.
_DEAD_PID = 2_147_483_646


def _insert_dead_lease(conn, owner: str = "autonomy") -> None:
    conn.execute(
        """
        INSERT INTO leases(owner, pid, host, acquired_at, expires_at, heartbeat_at)
        VALUES (?, ?, ?, ?, ?, ?);
        """,
        (
            owner,
            _DEAD_PID,
            socket.gethostname(),
            "2026-01-01T00:00:00.000Z",
            "2026-01-01T00:05:00.000Z",
            "2026-01-01T00:00:00.000Z",
        ),
    )
    conn.commit()


def _lease_count(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM leases;").fetchone()[0])


def test_doctor_repair_dry_run_does_not_clear_stale_lease(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths.ensure()
    conn = init_db(paths.db)
    try:
        _insert_dead_lease(conn)
        events = EventLog(conn, paths)

        result = repair.repair(conn, paths, events, apply=False)

        assert result["dry_run"] is True
        assert any(f["class"] == "stale_lease" for f in result["findings"])
        # Dry-run must not mutate.
        assert result["applied"] == []
        assert _lease_count(conn) == 1
    finally:
        conn.close()


def test_doctor_repair_apply_clears_stale_lease_without_prompt(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths.ensure()
    conn = init_db(paths.db)
    try:
        _insert_dead_lease(conn)
        events = EventLog(conn, paths)

        result = repair.repair(conn, paths, events, apply=True)

        assert result["dry_run"] is False
        assert any(a["class"] == "stale_lease" for a in result["applied"])
        # The dead lease is gone.
        assert _lease_count(conn) == 0
    finally:
        conn.close()


def test_up_auto_repair_safe_only_clears_stale_lease(tmp_path: Path) -> None:
    """`up --auto-repair` applies the safe-only subset (stale lease included)."""
    paths = runtime_paths(tmp_path)
    paths.ensure()
    conn = init_db(paths.db)
    try:
        _insert_dead_lease(conn)
        events = EventLog(conn, paths)

        result = repair.repair(conn, paths, events, apply=True, safe_only=True)

        assert "stale_lease" in {a["class"] for a in result["applied"]}
        assert _lease_count(conn) == 0
    finally:
        conn.close()
