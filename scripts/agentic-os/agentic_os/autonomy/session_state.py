"""AutonomyManager + start/stop/pause/resume session APIs (issue #292)."""
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
# session_state→loop is a cycle; lazy import in start_session (#292).
from .preflight import preflight_check



@dataclass
class _SessionState:
    session_id: str
    started_at: str
    expected_finish_at: str
    max_minutes: int
    status: str = "running"  # running | paused | finished | stopped | failed
    # Issue #265 — bounded ring buffer: the dashboard copies this on every
    # poll (`list(sess.events_log)`), so an unbounded list grew without limit
    # over a long session. A deque(maxlen=...) keeps only the most recent
    # entries; readers iterate/copy, never slice, so the type swap is safe.
    events_log: Deque[Dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=_EVENTS_LOG_RING_SIZE)
    )
    # Issue #265 — durable running counters. The bounded `events_log` drops
    # old entries, so finalizing the session by re-scanning it would
    # undercount long runs. These accumulate over the whole session and feed
    # `finalize_session` directly.
    processed_work_items: Set[str] = field(default_factory=set)
    block_count: int = 0
    failure_count: int = 0
    finished_at: Optional[str] = None
    error: Optional[str] = None
    awaiting_task: bool = False
    paused_reason: Optional[str] = None
    preflight: Optional[Dict[str, Any]] = None
    paused_at: Optional[str] = None

class AutonomyManager:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._session: Optional[_SessionState] = None
        self._stop_event: Optional[threading.Event] = None
        self._pause_event: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None

    def start(self, paths: RuntimePaths, *, max_minutes: int) -> _SessionState:
        with self.lock:
            if self._session is not None and self._is_alive_locked():
                return self._session
            preflight = self._preflight(paths)
            session_id = "autonomy-" + uuid.uuid4().hex[:12]
            expected = (datetime.now(timezone.utc) + timedelta(minutes=max_minutes)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            session = _SessionState(
                session_id=session_id,
                started_at=_now_iso(),
                expected_finish_at=expected,
                max_minutes=max_minutes,
                preflight=preflight,
            )
            stop_event = threading.Event()
            pause_event = threading.Event()
            from .loop import _run_loop  # lazy — see top-of-module note
            thread = threading.Thread(
                target=_run_loop,
                args=(paths, session, stop_event, pause_event),
                daemon=False,
                name=f"autonomy-{session_id}",
            )
            self._session = session
            self._stop_event = stop_event
            self._pause_event = pause_event
            self._thread = thread
            thread.start()
            self._install_notifications(paths)
            return session

    def stop(self) -> Dict[str, Any]:
        with self.lock:
            stop_event = self._stop_event
            pause_event = self._pause_event
            thread = self._thread
            if stop_event is not None:
                stop_event.set()
            # Releasing the pause gate lets a paused worker observe the
            # stop signal and exit instead of sleeping out its poll.
            if pause_event is not None:
                pause_event.clear()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_SHUTDOWN_GRACE_SECONDS)
        try:
            from ..notifications import uninstall as _notif_uninstall
            _notif_uninstall()
        except Exception:
            pass
        return self.status()

    @staticmethod
    def _install_notifications(paths: RuntimePaths) -> None:
        """Issue #268 — wire the push-notification dispatcher for this session.

        Best-effort: a config/import failure must never block session start.
        """
        try:
            from ..config import load_or_default
            from ..notifications import install as _notif_install

            cfg = load_or_default(paths.repo_root)
            _notif_install(cfg.raw, paths=paths)
        except Exception:
            pass

    def pause(self) -> Dict[str, Any]:
        """Cooperatively pause the worker between pipeline steps.

        Issue #266 — the worker finishes its current step, flips status to
        ``paused`` and parks on the pause gate. State (DB connection, session
        identity) is retained so ``resume`` continues the same session.
        """
        with self.lock:
            if not self._is_alive_locked():
                return self.status()
            sess = self._session
            if sess is not None and sess.status == "running":
                self._pause_event.set()  # type: ignore[union-attr]
        return self.status()

    def resume(self) -> Dict[str, Any]:
        with self.lock:
            if self._pause_event is not None:
                self._pause_event.clear()
        return self.status()

    def is_active(self) -> bool:
        with self.lock:
            return (
                self._session is not None
                and self._session.status == "running"
                and self._thread is not None
                and self._thread.is_alive()
            )

    def status(self) -> Dict[str, Any]:
        with self.lock:
            if self._session is None:
                return {"active": False, "session": None}
            sess = self._session
            thread_alive = self._thread is not None and self._thread.is_alive()
            alive = self._is_alive_locked()
            if sess.status in ("running", "paused") and not thread_alive:
                sess.status = "failed"
                sess.finished_at = sess.finished_at or _now_iso()
                sess.error = sess.error or "worker thread is not alive"
            seconds_left = None
            if sess.status == "running":
                try:
                    expected_dt = datetime.strptime(
                        sess.expected_finish_at, "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc)
                    seconds_left = max(0, int((expected_dt - datetime.now(timezone.utc)).total_seconds()))
                except ValueError:
                    seconds_left = None
            return {
                "active": sess.status == "running" and alive,
                "paused": sess.status == "paused" and thread_alive,
                "session": {
                    "session_id": sess.session_id,
                    "status": sess.status,
                    "started_at": sess.started_at,
                    "expected_finish_at": sess.expected_finish_at,
                    "finished_at": sess.finished_at,
                    "max_minutes": sess.max_minutes,
                    "seconds_left": seconds_left,
                    "events_log": list(sess.events_log),
                    "error": sess.error,
                    "awaiting_task": sess.awaiting_task and sess.status == "running",
                    "paused_reason": sess.paused_reason if sess.status == "running" else None,
                    "paused_at": sess.paused_at if sess.status == "paused" else None,
                    "preflight": sess.preflight,
                },
            }

    def _is_alive_locked(self) -> bool:
        # `paused` is a live session — the worker thread is parked on the
        # pause gate, not dead. Only `running` counts as actively driving
        # the pipeline; callers needing the distinction read `status`.
        return (
            self._session is not None
            and self._session.status in ("running", "paused")
            and self._thread is not None
            and self._thread.is_alive()
        )

    @staticmethod
    def _preflight(paths: RuntimePaths) -> Dict[str, Any]:
        try:
            return preflight_check(paths)
        except Exception as exc:  # intentionally broad: preflight must never crash session start — any failure becomes a structured "fail" check
            return {
                "ok": False,
                "checks": [{
                    "id": "preflight",
                    "status": "fail",
                    "message": f"preflight raised: {exc}",
                    "actions": ["Inspect the dashboard server log."],
                }],
            }

_MANAGER = AutonomyManager()

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def start_session(paths: RuntimePaths, *, max_minutes: int) -> _SessionState:
    """Start a new autonomy session. No-op (returns active) if one is already running."""
    return _MANAGER.start(paths, max_minutes=max_minutes)

def stop_session() -> Dict[str, Any]:
    """Signal the worker to stop. Returns last known status."""
    return _MANAGER.stop()

def pause_session() -> Dict[str, Any]:
    """Pause the running worker between steps. Returns last known status."""
    return _MANAGER.pause()

def resume_session() -> Dict[str, Any]:
    """Resume a paused worker. Returns last known status."""
    return _MANAGER.resume()

def is_session_active() -> bool:
    """True while an autonomy session is in the running state."""
    return _MANAGER.is_active()

def current_status() -> Dict[str, Any]:
    """Return JSON-serializable status. Empty dict when no session ever ran."""
    return _MANAGER.status()

def _record(session: _SessionState, step: str, ok: bool, detail: str = "") -> None:
    from ..sessions import classify_event

    entry = {
        "ts": _now_iso(),
        "step": step,
        "ok": ok,
        "detail": detail[:_RECORD_DETAIL_MAX_CHARS],
    }
    with _MANAGER.lock:
        session.events_log.append(entry)
        # Accumulate durable counters before the ring buffer can drop the
        # entry (issue #265). Same classifier as counts_from_events_log.
        work_item, is_block, is_failure = classify_event(entry)
        if work_item:
            session.processed_work_items.add(work_item)
        if is_block:
            session.block_count += 1
        elif is_failure:
            session.failure_count += 1


def _record_block_reason(session: _SessionState, reason: str) -> None:
    should_record = False
    with _MANAGER.lock:
        if session.paused_reason != reason:
            session.paused_reason = reason
            should_record = True
    if should_record:
        _record(session, "idle:blocked", False, reason)
