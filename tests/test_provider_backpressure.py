"""Issue #361 — backpressure bridge.

A role is backpressured only when its ENTIRE provider failover chain is on
cooldown — one cold provider is not backpressure, the chain still has alive
entries. ``all_providers_cold`` is the bridge a consumer composes into a
``ConcurrencyController`` backpressure_check.
"""
from __future__ import annotations

from pathlib import Path

from agentic_os.models.failover import all_providers_cold, mark_cooldown
from agentic_os.paths import runtime_paths
from agentic_os.storage.db import init_db

_PRIMARY = {"provider": "claude"}
_FALLBACK = [{"provider": "codex"}]


def test_not_backpressured_when_chain_has_alive_provider(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths.ensure()
    conn = init_db(paths.db)
    try:
        # Only the primary is cold; the fallback is still alive.
        mark_cooldown(conn, role="planner", provider="claude", trigger="rate_limit")
        assert all_providers_cold(conn, primary=_PRIMARY, fallback=_FALLBACK, role="planner") is False
    finally:
        conn.close()


def test_backpressured_when_whole_chain_cold(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths.ensure()
    conn = init_db(paths.db)
    try:
        mark_cooldown(conn, role="planner", provider="claude", trigger="rate_limit")
        mark_cooldown(conn, role="planner", provider="codex", trigger="rate_limit")
        assert all_providers_cold(conn, primary=_PRIMARY, fallback=_FALLBACK, role="planner") is True
    finally:
        conn.close()
