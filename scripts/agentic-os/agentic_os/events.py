"""Event writer for SQLite + NDJSON dual log.

Honours the write order from docs/database-schema.md section 5:
1. domain change inside a transaction (caller's responsibility)
2. insert events row with ndjson_file=NULL, ndjson_offset=NULL
3. commit
4. append JSON line to agentic-os-runtime/events/YYYY-MM-DD.ndjson
5. update events.ndjson_file and events.ndjson_offset
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .ids import ulid
from .paths import RuntimePaths
from .time_utils import now_iso, today_iso_date


_VALID_SEVERITY = {"info", "warning", "error"}

# Issue #268 — observers notified after every successful event write. The
# notification dispatcher subscribes here. Observers MUST NOT block or raise:
# `write()` invokes them best-effort and swallows exceptions so a misbehaving
# subscriber can never stall or crash the orchestrator's write path.
_SUBSCRIBERS: list = []


def subscribe(observer) -> None:
    """Register an event observer ``observer(event: Event) -> None``."""
    if observer not in _SUBSCRIBERS:
        _SUBSCRIBERS.append(observer)


def unsubscribe(observer) -> None:
    if observer in _SUBSCRIBERS:
        _SUBSCRIBERS.remove(observer)


def clear_subscribers() -> None:
    _SUBSCRIBERS.clear()


def _notify_subscribers(event: "Event") -> None:
    for observer in list(_SUBSCRIBERS):
        try:
            observer(event)
        except Exception:
            # An observer failure must never propagate to the write path.
            pass

# Issue #245 — canonical step.* taxonomy. Every autonomous code path emits
# step.start / step.end (and throttled step.progress) so the dashboard and
# CLI follow can share one schema. Existing event kinds stay untouched.
_STEP_KINDS = {
    "planner",
    "implementer",
    "reviewer",
    "triager",
    "generator",
    "gate",
    "git",
    "run-tests",
    "exploratory",
}
_STEP_PHASES = {
    "analyze",
    "design",
    "implement",
    "review",
    "triage",
    "generate",
    "gate",
    "run",
    "report",
}
_STEP_OUTCOMES = {"ok", "blocked", "failed"}
_STEP_ROLES = {"planner", "implementer", "reviewer", "triager", None}
_STEP_PROVIDERS = {"claude", "codex", "antigravity", "script", None}
_DEFAULT_PROGRESS_THROTTLE = 5
_PROGRESS_WINDOW_SECONDS = 1.0


class StepSchemaError(ValueError):
    """Raised when a step.* payload violates the documented taxonomy."""


@dataclass(frozen=True)
class Event:
    id: str
    ts: str
    kind: str
    actor: str
    severity: str
    payload: Dict[str, Any]
    run_id: Optional[str] = None
    phase_id: Optional[str] = None
    task_id: Optional[str] = None


class EventLog:
    def __init__(
        self,
        conn: sqlite3.Connection,
        paths: RuntimePaths,
        *,
        step_progress_throttle: int = _DEFAULT_PROGRESS_THROTTLE,
    ):
        self._conn = conn
        self._paths = paths
        self._paths.events_dir.mkdir(parents=True, exist_ok=True)
        if step_progress_throttle < 0:
            raise ValueError("step_progress_throttle must be >= 0")
        self._step_progress_throttle = step_progress_throttle
        # step_id -> (window_start_monotonic, emitted_in_window)
        self._progress_buckets: Dict[str, tuple[float, int]] = {}
        # step_id -> started_at iso, for end_step duration computation
        self._step_started: Dict[str, str] = {}

    @property
    def paths(self) -> RuntimePaths:
        return self._paths

    def write(
        self,
        kind: str,
        *,
        actor: str = "orchestrator",
        severity: str = "info",
        payload: Optional[Dict[str, Any]] = None,
        run_id: Optional[str] = None,
        phase_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Event:
        if severity not in _VALID_SEVERITY:
            raise ValueError(f"invalid event severity: {severity}")
        event = Event(
            id=ulid(),
            ts=now_iso(),
            kind=kind,
            actor=actor,
            severity=severity,
            payload=payload or {},
            run_id=run_id,
            phase_id=phase_id,
            task_id=task_id,
        )
        self._insert(event)
        self._append_ndjson(event)
        _notify_subscribers(event)
        return event

    def _insert(self, event: Event) -> None:
        self._conn.execute(
            """
            INSERT INTO events(id, ts, run_id, phase_id, task_id, kind, actor, severity, payload, ndjson_file, ndjson_offset)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL);
            """,
            (
                event.id,
                event.ts,
                event.run_id,
                event.phase_id,
                event.task_id,
                event.kind,
                event.actor,
                event.severity,
                json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
            ),
        )

    def _append_ndjson(self, event: Event) -> None:
        date = today_iso_date()
        fname = f"{date}.ndjson"
        target = self._paths.events_dir / fname
        line = json.dumps(
            {
                "id": event.id,
                "ts": event.ts,
                "run_id": event.run_id,
                "phase_id": event.phase_id,
                "task_id": event.task_id,
                "kind": event.kind,
                "actor": event.actor,
                "severity": event.severity,
                "payload": event.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n"
        with target.open("ab") as fh:
            offset = fh.tell()
            fh.write(line.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        self._update_current_symlink(target)
        try:
            self._conn.execute(
                "UPDATE events SET ndjson_file=?, ndjson_offset=? WHERE id=?;",
                (str(target.relative_to(self._paths.repo_root)), offset, event.id),
            )
        except sqlite3.Error:
            # Domain change is already committed; the recovery scan reconciles ndjson columns.
            pass

    def _update_current_symlink(self, target: Path) -> None:
        link = self._paths.events_dir / "current"
        # Issue #361 — atomic replace. The previous unlink-then-symlink had a
        # gap two concurrent writers could interleave on: the loser of the
        # symlink race fell back to `link.write_text(...)`, which FOLLOWS the
        # `current` symlink the winner just created and truncates the live
        # NDJSON file it points at (catastrophic cross-process data loss). We
        # build the new link under a unique temp name and `os.replace` it onto
        # `current` — an atomic rename that swaps the symlink without ever
        # writing through it.
        tmp = self._paths.events_dir / f".current.{uuid.uuid4().hex}.tmp"
        try:
            os.symlink(target.name, tmp)
            os.replace(tmp, link)
        except OSError:
            # Filesystems without symlink support: write a regular marker file,
            # still atomically and never through `current` itself.
            try:
                tmp.unlink()
            except OSError:
                pass
            try:
                tmp.write_text(target.name, encoding="utf-8")
                os.replace(tmp, link)
            except OSError:
                try:
                    tmp.unlink()
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # step.* taxonomy — issue #245
    # ------------------------------------------------------------------

    @property
    def step_progress_throttle(self) -> int:
        return self._step_progress_throttle

    def start_step(
        self,
        *,
        kind: str,
        phase: str,
        actor: str,
        role: Optional[str] = None,
        provider: Optional[str] = None,
        skill: Optional[str] = None,
        work_item_id: Optional[str] = None,
        candidate_id: Optional[str] = None,
        parent_step_id: Optional[str] = None,
        detail: str = "",
        log_ref: Optional[str] = None,
        run_id: Optional[str] = None,
        phase_id: Optional[str] = None,
        task_id: Optional[str] = None,
        step_id: Optional[str] = None,
    ) -> str:
        sid = step_id or uuid.uuid4().hex
        started_at = now_iso()
        payload = {
            "step_id": sid,
            "parent_step_id": parent_step_id,
            "kind": kind,
            "phase": phase,
            "actor": actor,
            "role": role,
            "provider": provider,
            "skill": skill,
            "work_item_id": work_item_id,
            "candidate_id": candidate_id,
            "started_at": started_at,
            "ended_at": None,
            "outcome": None,
            "detail": detail,
            "log_ref": log_ref,
        }
        _validate_step_payload(payload, expect_terminal=False)
        self._step_started[sid] = started_at
        self.write(
            "step.start",
            actor=actor,
            payload=payload,
            run_id=run_id,
            phase_id=phase_id,
            task_id=task_id,
        )
        return sid

    def end_step(
        self,
        step_id: str,
        *,
        outcome: str,
        kind: str,
        phase: str,
        actor: str,
        role: Optional[str] = None,
        provider: Optional[str] = None,
        skill: Optional[str] = None,
        work_item_id: Optional[str] = None,
        candidate_id: Optional[str] = None,
        parent_step_id: Optional[str] = None,
        detail: str = "",
        log_ref: Optional[str] = None,
        exit_code: Optional[int] = None,
        run_id: Optional[str] = None,
        phase_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Event:
        started_at = self._step_started.pop(step_id, now_iso())
        ended_at = now_iso()
        severity = "info" if outcome == "ok" else ("warning" if outcome == "blocked" else "error")
        payload = {
            "step_id": step_id,
            "parent_step_id": parent_step_id,
            "kind": kind,
            "phase": phase,
            "actor": actor,
            "role": role,
            "provider": provider,
            "skill": skill,
            "work_item_id": work_item_id,
            "candidate_id": candidate_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "outcome": outcome,
            "detail": detail,
            "log_ref": log_ref,
            "exit_code": exit_code,
        }
        _validate_step_payload(payload, expect_terminal=True)
        # Drop throttle bucket — step is done.
        self._progress_buckets.pop(step_id, None)
        return self.write(
            "step.end",
            actor=actor,
            severity=severity,
            payload=payload,
            run_id=run_id,
            phase_id=phase_id,
            task_id=task_id,
        )

    def progress_step(
        self,
        step_id: str,
        message: str,
        *,
        actor: str = "orchestrator",
        log_ref: Optional[str] = None,
        run_id: Optional[str] = None,
        phase_id: Optional[str] = None,
        task_id: Optional[str] = None,
        max_message_chars: int = 512,
    ) -> Optional[Event]:
        """Emit a throttled step.progress event. Returns None when dropped."""
        if self._step_progress_throttle == 0:
            return None
        now_mono = time.monotonic()
        window_start, count = self._progress_buckets.get(step_id, (now_mono, 0))
        if now_mono - window_start >= _PROGRESS_WINDOW_SECONDS:
            window_start, count = now_mono, 0
        if count >= self._step_progress_throttle:
            self._progress_buckets[step_id] = (window_start, count)
            return None
        self._progress_buckets[step_id] = (window_start, count + 1)
        snippet = message if len(message) <= max_message_chars else message[:max_message_chars] + "…"
        payload = {
            "step_id": step_id,
            "ts": now_iso(),
            "message": snippet,
            "log_ref": log_ref,
        }
        return self.write(
            "step.progress",
            actor=actor,
            payload=payload,
            run_id=run_id,
            phase_id=phase_id,
            task_id=task_id,
        )

    def tail(self, lines: int = 20) -> list[Dict[str, Any]]:
        files = sorted(self._paths.events_dir.glob("*.ndjson"))
        collected: list[Dict[str, Any]] = []
        for path in reversed(files):
            for raw in reversed(path.read_text(encoding="utf-8").splitlines()):
                if not raw:
                    continue
                try:
                    collected.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
                if len(collected) >= lines:
                    break
            if len(collected) >= lines:
                break
        return list(reversed(collected))


def event_log_from_config(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    config: Optional[Dict[str, Any]] = None,
) -> EventLog:
    """Construct an EventLog using the `events.step_progress_throttle` config knob."""
    throttle = _DEFAULT_PROGRESS_THROTTLE
    if isinstance(config, dict):
        events_cfg = config.get("events")
        if isinstance(events_cfg, dict):
            raw = events_cfg.get("step_progress_throttle")
            if isinstance(raw, int) and raw >= 0:
                throttle = raw
    return EventLog(conn, paths, step_progress_throttle=throttle)


def event_log_for_paths(conn: sqlite3.Connection, paths: RuntimePaths) -> EventLog:
    """Best-effort factory used by runtime entry points.

    Loads the project config from `paths.repo_root` so the runtime
    actually honours `events.step_progress_throttle`. When config
    loading fails for any reason (missing file, validation error during
    bootstrap) we fall back to the default throttle so the orchestrator
    still functions — the dashboard's preflight/doctor surfaces the
    config error separately. Codex PR #276 review (P2).
    """
    try:
        from .config import load_or_default

        cfg = load_or_default(paths.repo_root)
        return event_log_from_config(conn, paths, cfg.raw)
    except Exception:
        return EventLog(conn, paths)


def _validate_step_payload(payload: Dict[str, Any], *, expect_terminal: bool) -> None:
    """Validate a step.start / step.end payload against the documented schema."""
    required = (
        "step_id",
        "parent_step_id",
        "kind",
        "phase",
        "actor",
        "role",
        "provider",
        "skill",
        "work_item_id",
        "candidate_id",
        "started_at",
        "ended_at",
        "outcome",
        "detail",
        "log_ref",
    )
    missing = [k for k in required if k not in payload]
    if missing:
        raise StepSchemaError(f"step payload missing fields: {missing}")
    if not isinstance(payload["step_id"], str) or not payload["step_id"]:
        raise StepSchemaError("step_id must be a non-empty string")
    if payload["parent_step_id"] is not None and not isinstance(payload["parent_step_id"], str):
        raise StepSchemaError("parent_step_id must be str or None")
    if payload["kind"] not in _STEP_KINDS:
        raise StepSchemaError(f"step.kind invalid: {payload['kind']!r}; allowed={sorted(_STEP_KINDS)}")
    if payload["phase"] not in _STEP_PHASES:
        raise StepSchemaError(f"step.phase invalid: {payload['phase']!r}; allowed={sorted(_STEP_PHASES)}")
    if not isinstance(payload["actor"], str) or not payload["actor"]:
        raise StepSchemaError("step.actor must be non-empty string")
    if payload["role"] not in _STEP_ROLES:
        raise StepSchemaError(f"step.role invalid: {payload['role']!r}; allowed={sorted(r for r in _STEP_ROLES if r)}∪{{null}}")
    if payload["provider"] not in _STEP_PROVIDERS:
        raise StepSchemaError(f"step.provider invalid: {payload['provider']!r}")
    if payload["skill"] is not None and not isinstance(payload["skill"], str):
        raise StepSchemaError("step.skill must be str or None")
    if payload["work_item_id"] is not None and not isinstance(payload["work_item_id"], str):
        raise StepSchemaError("step.work_item_id must be str or None")
    if payload["candidate_id"] is not None and not isinstance(payload["candidate_id"], str):
        raise StepSchemaError("step.candidate_id must be str or None")
    if not isinstance(payload["started_at"], str) or not payload["started_at"]:
        raise StepSchemaError("step.started_at must be non-empty ISO-8601 string")
    if expect_terminal:
        if not isinstance(payload["ended_at"], str) or not payload["ended_at"]:
            raise StepSchemaError("step.end payload requires non-empty ended_at")
        if payload["outcome"] not in _STEP_OUTCOMES:
            raise StepSchemaError(
                f"step.outcome invalid: {payload['outcome']!r}; allowed={sorted(_STEP_OUTCOMES)}"
            )
    else:
        if payload["ended_at"] is not None:
            raise StepSchemaError("step.start payload must have ended_at=null")
        if payload["outcome"] is not None:
            raise StepSchemaError("step.start payload must have outcome=null")
    if not isinstance(payload["detail"], str):
        raise StepSchemaError("step.detail must be string (may be empty)")
    if payload["log_ref"] is not None and not isinstance(payload["log_ref"], str):
        raise StepSchemaError("step.log_ref must be str or None")
