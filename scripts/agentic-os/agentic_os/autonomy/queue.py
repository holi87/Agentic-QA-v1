"""Task queue policy + paused-state helpers (issue #292)."""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set

from .. import task_synthesis
from ..events import EventLog, event_log_for_paths
from ..paths import RuntimePaths
from ..runtime.tuning import (
    EVENTS_LOG_RING_SIZE as _EVENTS_LOG_RING_SIZE,
    RECORD_DETAIL_MAX_CHARS as _RECORD_DETAIL_MAX_CHARS,
    SHUTDOWN_GRACE_SECONDS as _SHUTDOWN_GRACE_SECONDS,
)
from ..storage import init_db
from .session_state import _MANAGER, _SessionState, _now_iso, _record



def _resolve_queue_policy(paths: RuntimePaths):
    """Issue #274 — read ``autonomy.queue_policy`` from config.

    Best-effort: any config load / parse failure falls back to the module
    default (HYBRID), which is itself behaviour-preserving. Returns a
    ``queue.QueuePolicy``.
    """
    from .. import queue as _queue

    try:
        from ..config import load_or_default

        cfg = load_or_default(paths.repo_root)
        raw = (cfg.raw.get("autonomy") or {}).get("queue_policy")
    except Exception:
        return _queue.DEFAULT_QUEUE_POLICY
    return _queue.coerce_policy(raw)

def _load_cfg_best_effort(paths: RuntimePaths):
    """Issue #290 — load config for the synthesis gate, never raising.

    Returns the ``Config`` or ``None`` on any failure so the empty-queue
    branch falls through to the unchanged exploratory/idle path.
    """
    try:
        from ..config import load_or_default

        return load_or_default(paths.repo_root)
    except Exception:
        return None

def _task_synthesis_enabled(cfg: Any) -> bool:
    """Issue #290 — ``autonomy.task_synthesis`` flag, default OFF."""
    raw = getattr(cfg, "raw", None)
    autonomy = (raw.get("autonomy") or {}) if isinstance(raw, dict) else {}
    return bool(autonomy.get("task_synthesis", False))

def _task_synthesis_cap(cfg: Any) -> int:
    """Issue #290 — ``autonomy.task_synthesis_max_per_cycle`` (default 3, min 1)."""
    raw = getattr(cfg, "raw", None)
    autonomy = (raw.get("autonomy") or {}) if isinstance(raw, dict) else {}
    value = autonomy.get("task_synthesis_max_per_cycle", 3)
    try:
        cap = int(value)
    except (TypeError, ValueError):
        cap = 3
    return max(1, cap)

def _resolve_active_project_best_effort(conn: Any, cfg: Any) -> str:
    """Issue #290 — active project id for synthesis, default on any failure."""
    from ..projects import DEFAULT_PROJECT_ID, resolve_active_project_id

    try:
        return resolve_active_project_id(conn, cfg)
    except Exception:
        return DEFAULT_PROJECT_ID

def _wait_if_paused(
    session: _SessionState,
    stop_event: threading.Event,
    pause_event: threading.Event,
) -> None:
    """Park the worker while the pause gate is set.

    Flips ``status`` to ``paused`` on entry and back to ``running`` on
    resume, recording both transitions. Returns immediately when stop is
    requested so the worker can exit instead of sleeping on a paused gate.
    """
    if not pause_event.is_set() or stop_event.is_set():
        return
    with _MANAGER.lock:
        if session.status == "running":
            session.status = "paused"
            session.paused_at = _now_iso()
    _record(session, "session.paused", True, "worker parked between steps")
    while pause_event.is_set() and not stop_event.is_set():
        stop_event.wait(timeout=1.0)
    with _MANAGER.lock:
        if session.status == "paused" and not stop_event.is_set():
            session.status = "running"
            session.paused_at = None
    if not stop_event.is_set():
        _record(session, "session.resumed", True, "worker resumed")
_PENDING_STATUSES = ("queued", "analyzing", "planned", "implementing")

def _select_pending(items: list) -> list:
    """Return work items the autonomy loop should drive this tick.

    Selects by `status` against `_PENDING_STATUSES`. Pure and side-effect free
    so the queue-selection contract is unit-testable without a running loop.
    """
    return [w for w in items if str(w.get("status") or "").lower() in _PENDING_STATUSES]
