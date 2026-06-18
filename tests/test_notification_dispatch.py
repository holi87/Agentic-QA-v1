"""Notification classification, config validation, deduplication, dispatch failure, and CLI test mode."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from agentic_os import events as events_mod
from agentic_os.events import EventLog
from agentic_os.notifications import (
    NotificationDispatcher,
    _event_to_notification,
    classify_event,
    send_test,
)
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db


class _Ev:
    def __init__(self, kind, payload=None, actor="orchestrator", ts="2026-05-26T00:00:00Z", task_id=None):
        self.kind = kind
        self.payload = payload or {}
        self.actor = actor
        self.ts = ts
        self.task_id = task_id


def _cfg(events=("blocked", "budget_exceeded"), dedup=300):
    return {
        "notifications": {
            "enabled": True,
            "dedup_window_seconds": dedup,
            "channels": {"webhook": {"url": "http://example.test/hook", "events": list(events)}},
        }
    }


# ---- classify ----


def test_classify_event_mapping() -> None:
    assert classify_event(_Ev("budget.exceeded")) == "budget_exceeded"
    assert classify_event(_Ev("provider_failover")) == "failover"
    assert classify_event(_Ev("provider_chain_exhausted")) == "provider_chain_exhausted"
    assert classify_event(_Ev("autonomy.completed")) == "session_completed"
    assert classify_event(_Ev("session.completed")) == "session_completed"
    assert classify_event(_Ev("step.end", {"outcome": "blocked"})) == "blocked"
    assert classify_event(_Ev("step.end", {"outcome": "ok"})) is None
    assert classify_event(_Ev("model.binary_missing")) is None


def test_session_completed_carries_summary_path() -> None:
    ev = _Ev(
        "autonomy.completed",
        {"session_id": "S1", "summary_path": "reports/session-summary-S1.md"},
    )
    notif = _event_to_notification(ev, "session_completed")
    assert notif["summary_path"] == "reports/session-summary-S1.md"
    assert notif["session_id"] == "S1"


# ---- config validation ----


def test_config_accepts_valid_notifications(tmp_path: Path) -> None:
    from agentic_os.config import _validate

    errs = _validate(_full_config())
    assert errs == []


def test_config_rejects_bad_event_kind() -> None:
    from agentic_os.config import _validate

    cfg = _full_config()
    cfg["notifications"]["channels"]["webhook"]["events"] = ["not_a_kind"]
    errs = _validate(cfg)
    assert any("notifications.channels.webhook.events" in e for e in errs)


def test_config_rejects_webhook_without_url() -> None:
    from agentic_os.config import _validate

    cfg = _full_config()
    del cfg["notifications"]["channels"]["webhook"]["url"]
    errs = _validate(cfg)
    assert any("notifications.channels.webhook.url" in e for e in errs)


# ---- dedup ----


def test_dedup_window_collapses_duplicates() -> None:
    captured = []
    d = NotificationDispatcher(
        _cfg(),
        senders={"webhook": lambda c, p: captured.append(p)},
    )
    ev = _Ev("budget.exceeded", {"work_item_id": "WI-1"})
    assert d.handle_event(ev) is True
    assert d.handle_event(ev) is False  # within window → skipped
    # drain queue synchronously
    d.dispatch(d._queue.get_nowait())
    assert len(captured) == 1


def test_dedup_window_expiry_allows_resend() -> None:
    clock = {"t": 1000.0}
    d = NotificationDispatcher(_cfg(dedup=10), clock=lambda: clock["t"])
    ev = _Ev("budget.exceeded", {"work_item_id": "WI-9"})
    assert d.handle_event(ev) is True
    clock["t"] += 11
    assert d.handle_event(ev) is True


# ---- bounded queue ----


def test_bounded_queue_drops_when_full() -> None:
    d = NotificationDispatcher(_cfg(dedup=0), queue_size=2)
    # dedup=0 means every event is eligible; distinct work items avoid dedup anyway.
    enq = 0
    for i in range(5):
        if d.handle_event(_Ev("budget.exceeded", {"work_item_id": f"WI-{i}"})):
            enq += 1
    assert enq == 2
    assert d.dropped == 3


# ---- dispatch failure ----


def test_dispatch_failure_records_and_does_not_raise() -> None:
    def boom(channel_cfg, payload):
        raise RuntimeError("webhook down")

    d = NotificationDispatcher(_cfg(), senders={"webhook": boom})
    d.dispatch({"event": "blocked", "work_item_id": "WI-3"})
    assert len(d.failures) == 1
    assert d.failures[0]["channel"] == "webhook"
    assert "webhook down" in d.failures[0]["error"]


def test_dispatch_failure_emits_notification_failed_event(tmp_path: Path) -> None:
    paths = RuntimePaths(repo_root=tmp_path / "repo", runtime_root=tmp_path / "repo" / ".agentic-os")
    paths.ensure()
    init_db(paths.db)

    def boom(channel_cfg, payload):
        raise RuntimeError("nope")

    d = NotificationDispatcher(_cfg(), paths=paths, senders={"webhook": boom})
    d.dispatch({"event": "blocked", "work_item_id": "WI-4"})
    # notification.failed should be in the events log.
    conn = init_db(paths.db)
    try:
        rows = conn.execute(
            "SELECT kind FROM events WHERE kind='notification.failed';"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) >= 1


# ---- subscriber integration ----


def test_subscriber_receives_event_writes(tmp_path: Path) -> None:
    paths = RuntimePaths(repo_root=tmp_path / "repo", runtime_root=tmp_path / "repo" / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    captured = []
    d = NotificationDispatcher(_cfg(), senders={"webhook": lambda c, p: captured.append(p)})
    events_mod.subscribe(d.handle_event)
    try:
        EventLog(conn, paths).write(
            "budget.exceeded", severity="error", payload={"work_item_id": "WI-5", "dimension": "session"}
        )
        # handle_event enqueued; drain synchronously.
        d.dispatch(d._queue.get_nowait())
    finally:
        events_mod.unsubscribe(d.handle_event)
        conn.close()
    assert len(captured) == 1
    assert captured[0]["event"] == "budget_exceeded"
    assert captured[0]["work_item_id"] == "WI-5"


def test_disabled_dispatcher_ignores_events() -> None:
    d = NotificationDispatcher({"notifications": {"enabled": False}})
    assert d.handle_event(_Ev("budget.exceeded")) is False


# ---- CLI test mode ----


def test_send_test_uses_channel(monkeypatch) -> None:
    import agentic_os.notifications as nm

    calls = []
    monkeypatch.setattr(nm, "_send_webhook", lambda c, p: calls.append(p))
    res = send_test(_cfg(), "webhook")
    assert res["ok"] is True
    assert len(calls) == 1
    assert calls[0]["event"] == "test"


def test_send_test_unknown_channel() -> None:
    res = send_test(_cfg(), "sound")
    assert res["ok"] is False


def _full_config():
    """A minimal-but-valid full config dict for the validator."""
    import copy

    base = copy.deepcopy(_BASE_CONFIG)
    base["notifications"] = {
        "enabled": True,
        "dedup_window_seconds": 120,
        "channels": {
            "webhook": {"url": "http://example.test/hook", "events": ["blocked", "budget_exceeded"]},
            "desktop": {"enabled": True, "events": ["blocked"]},
            "sound": {"enabled": True, "events": ["blocked"]},
        },
    }
    return base


_BASE_CONFIG = {
    "runtime": {
        "root": "agentic-os-runtime",
        "timezone": "Europe/Warsaw",
        "max_parallel_tasks": 1,
        "heartbeat_seconds": 10,
        "lease_ttl_seconds": 600,
        "stale_lease_seconds": 1800,
        "shutdown_grace_seconds": 30,
        "timeouts": {
            "default_seconds": 600,
            "docker_seconds": 120,
            "test_seconds": 900,
            "model_seconds": 600,
            "report_seconds": 120,
        },
    },
    "sut": {
        "root": ".",
        "compose_file": "docker-compose.yml",
        "compose_project_name": "app",
        "autostart": False,
        "healthcheck": {"command": ["sh", "-c", "exit 0"], "timeout_seconds": 5, "retries": 1},
        "test_runner": "scripts/run-tests.sh",
        "install_shim_allowed": False,
    },
    "models": {
        "planner": {"provider": "claude", "command": ["claude"], "role": "opus"},
        "implementer": {"provider": "claude", "command": ["claude"], "role": "sonnet"},
        "reviewer": {"provider": "codex", "command": ["codex"], "role": "codex"},
    },
    "dashboard": {"host": "127.0.0.1", "port": 8765, "enable_write_endpoints": False},
    "paths": {"reports": "reports", "bugs": "bugs", "evidence": "evidence", "prompts": "prompts"},
    "reports": {
        "copy_reports_script": "scripts/copy-reports.sh",
        "extract_last_run_script": "scripts/extract-last-run.sh",
        "build_summary_script": "scripts/build-summary.sh",
        "require_reports_on_failure": True,
    },
    "gates": {
        "known_bugs_fail_exit": True,
        "assertion_changes_require_decision": True,
        "exact_spec_failure_opens_bug": True,
        "require_functional_area_tag": True,
        "require_lifecycle_tag": True,
        "infrastructure_exit_code": 2,
    },
}
