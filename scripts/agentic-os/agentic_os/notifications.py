"""Issue #268 — push notifications on blocked / completed / budget / failover.

Full autonomy assumes the operator is not watching the dashboard. When the
loop blocks (provider chain exhausted, budget exceeded, gate REJECT with no
auto-recovery) the operator needs a push, not a poll.

The dispatcher subscribes to the EventLog write path (`events.subscribe`).
It NEVER blocks the orchestrator: `handle_event` only classifies, dedups and
enqueues onto a bounded queue; a daemon worker thread drains the queue and
calls the channel adapters. A dispatch failure emits a `notification.failed`
event and is swallowed — it is never raised back into the loop.
"""
from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .time_utils import now_iso

# The notification kinds the operator can subscribe channels to.
NOTIFICATION_KINDS = frozenset({
    "blocked",
    "budget_exceeded",
    "session_completed",
    "provider_chain_exhausted",
    "failover",
})

_DEFAULT_DEDUP_WINDOW_SECONDS = 300
_DEFAULT_QUEUE_SIZE = 256


def classify_event(event: Any) -> Optional[str]:
    """Map an agentic Event to a notification kind, or None when irrelevant."""
    kind = getattr(event, "kind", None)
    payload = getattr(event, "payload", {}) or {}
    if kind == "budget.exceeded":
        return "budget_exceeded"
    if kind == "provider_chain_exhausted":
        return "provider_chain_exhausted"
    if kind == "provider_failover":
        return "failover"
    if kind in ("session.completed", "autonomy.completed"):
        return "session_completed"
    if kind == "step.end" and payload.get("outcome") == "blocked":
        return "blocked"
    return None


def _event_to_notification(event: Any, notif_kind: str) -> Dict[str, Any]:
    payload = getattr(event, "payload", {}) or {}
    work_item_id = (
        payload.get("work_item_id")
        or getattr(event, "task_id", None)
    )
    reason = payload.get("reason") or payload.get("dimension") or payload.get("trigger") or notif_kind
    return {
        "event": notif_kind,
        "session_id": payload.get("session_id"),
        "work_item_id": work_item_id,
        "reason": reason,
        "actor": getattr(event, "actor", None),
        "timestamp": getattr(event, "ts", None) or now_iso(),
        # Issue #272 — the session-completion event carries the handoff doc
        # path so an operator can jump straight to the PR-ready summary.
        "summary_path": payload.get("summary_path"),
    }


# --------------------------------------------------------------------------
# Channel adapters — each takes (channel_cfg, payload) and raises on failure.
# --------------------------------------------------------------------------


def _send_webhook(channel_cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
    url = channel_cfg.get("url")
    if not url:
        raise ValueError("webhook channel missing url")
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 — operator-configured URL
        if resp.status >= 400:
            raise RuntimeError(f"webhook returned HTTP {resp.status}")


def _send_desktop(channel_cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
    title = "Agentic OS — " + str(payload.get("event", "notification"))
    body = f"{payload.get('reason', '')} ({payload.get('work_item_id') or '—'})"
    if sys.platform == "darwin" and shutil.which("osascript"):
        script = f'display notification {json.dumps(body)} with title {json.dumps(title)}'
        subprocess.run(["osascript", "-e", script], check=True, timeout=10)
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", title, body], check=True, timeout=10)
    else:
        raise RuntimeError("no desktop notifier available (osascript/notify-send)")


def _send_sound(channel_cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
    path = channel_cfg.get("path")
    if sys.platform == "darwin" and shutil.which("afplay"):
        target = path or "/System/Library/Sounds/Ping.aiff"
        subprocess.run(["afplay", target], check=True, timeout=10)
    elif shutil.which("paplay") and path:
        subprocess.run(["paplay", path], check=True, timeout=10)
    elif shutil.which("printf"):
        # Terminal bell fallback — always available, never the operator's wish
        # but better than a silent no-op when no audio backend exists.
        sys.stderr.write("\a")
        sys.stderr.flush()
    else:
        raise RuntimeError("no sound backend available")


class NotificationDispatcher:
    def __init__(
        self,
        config: Optional[Dict[str, Any]],
        *,
        paths: Any = None,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
        clock: Callable[[], float] = time.time,
        senders: Optional[Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], None]]] = None,
    ) -> None:
        notif = ((config or {}).get("notifications") or {}) if isinstance(config, dict) else {}
        self.enabled = bool(notif.get("enabled"))
        self.dedup_window = int(notif.get("dedup_window_seconds", _DEFAULT_DEDUP_WINDOW_SECONDS))
        self.channels: Dict[str, Any] = notif.get("channels") or {}
        self.paths = paths
        self._clock = clock
        self._queue: "queue.Queue" = queue.Queue(maxsize=queue_size)
        self._dedup: Dict[tuple, float] = {}
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._senders = senders or {
            "webhook": _send_webhook,
            "desktop": _send_desktop,
            "sound": _send_sound,
        }
        # Test/observability hooks — appended to even when no events conn.
        self.sent: List[Dict[str, Any]] = []
        self.failures: List[Dict[str, Any]] = []
        self.dropped = 0

    # -- subscriber callback ------------------------------------------------

    def handle_event(self, event: Any) -> bool:
        """EventLog subscriber. Cheap + non-blocking. Returns True when enqueued."""
        if not self.enabled:
            return False
        notif_kind = classify_event(event)
        if notif_kind is None:
            return False
        payload = _event_to_notification(event, notif_kind)
        if not self._should_send(notif_kind, payload.get("work_item_id")):
            return False
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            # Bounded queue: drop rather than block the write path.
            self.dropped += 1
            return False
        return True

    def _should_send(self, notif_kind: str, work_item_id: Optional[str]) -> bool:
        key = (notif_kind, work_item_id)
        now = self._clock()
        with self._lock:
            last = self._dedup.get(key)
            if last is not None and (now - last) < self.dedup_window:
                return False
            self._dedup[key] = now
        return True

    # -- worker -------------------------------------------------------------

    def start(self) -> None:
        if not self.enabled:
            return
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, daemon=True, name="notifications")
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            self.dispatch(item)

    # -- dispatch -----------------------------------------------------------

    def dispatch(self, payload: Dict[str, Any]) -> None:
        """Send one notification to every channel subscribed to its event kind."""
        notif_kind = payload.get("event")
        for channel_name, channel_cfg in self.channels.items():
            if not isinstance(channel_cfg, dict):
                continue
            if channel_name == "desktop" and not channel_cfg.get("enabled", True):
                continue
            if channel_name == "sound" and not channel_cfg.get("enabled", True):
                continue
            events = channel_cfg.get("events")
            if isinstance(events, list) and notif_kind not in events:
                continue
            self._send_one(channel_name, channel_cfg, payload)

    def _send_one(self, channel_name: str, channel_cfg: Dict[str, Any], payload: Dict[str, Any]) -> None:
        sender = self._senders.get(channel_name)
        if sender is None:
            return
        try:
            sender(channel_cfg, payload)
            self.sent.append({"channel": channel_name, "payload": payload})
        except Exception as exc:  # noqa: BLE001 — must never escape
            record = {"channel": channel_name, "event": payload.get("event"), "error": str(exc)}
            self.failures.append(record)
            self._emit_failed(record)

    def _emit_failed(self, record: Dict[str, Any]) -> None:
        """Best-effort `notification.failed` event. Never raises."""
        if self.paths is None:
            return
        try:
            from .events import EventLog
            from .storage import init_db

            conn = init_db(self.paths.db)
            try:
                EventLog(conn, self.paths).write(
                    "notification.failed", severity="warning", payload=record
                )
            finally:
                conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Module singleton — install once at orchestrator/dashboard startup.
# --------------------------------------------------------------------------

_ACTIVE: Optional[NotificationDispatcher] = None


def install(config: Optional[Dict[str, Any]], *, paths: Any = None) -> NotificationDispatcher:
    """Build, subscribe and start the dispatcher. Idempotent (replaces prior)."""
    from . import events as events_mod

    global _ACTIVE
    uninstall()
    dispatcher = NotificationDispatcher(config, paths=paths)
    if dispatcher.enabled:
        events_mod.subscribe(dispatcher.handle_event)
        dispatcher.start()
    _ACTIVE = dispatcher
    return dispatcher


def uninstall() -> None:
    from . import events as events_mod

    global _ACTIVE
    if _ACTIVE is not None:
        events_mod.unsubscribe(_ACTIVE.handle_event)
        _ACTIVE.stop()
        _ACTIVE = None


def send_test(config: Optional[Dict[str, Any]], channel: str, *, paths: Any = None) -> Dict[str, Any]:
    """`agentic-os notifications test` — synchronously fire a synthetic event."""
    dispatcher = NotificationDispatcher(config, paths=paths)
    channel_cfg = dispatcher.channels.get(channel)
    if not isinstance(channel_cfg, dict):
        return {"ok": False, "error": f"channel not configured: {channel}"}
    payload = {
        "event": "test",
        "session_id": None,
        "work_item_id": None,
        "reason": "notifications test",
        "actor": "operator",
        "timestamp": now_iso(),
    }
    dispatcher._send_one(channel, channel_cfg, payload)
    if dispatcher.failures:
        return {"ok": False, "error": dispatcher.failures[-1]["error"], "channel": channel}
    return {"ok": True, "channel": channel}
