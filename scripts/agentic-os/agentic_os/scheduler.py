"""Cron-style schedules for autonomous runs (issue #271).

This module bundles three cohesive concerns:

1. A vendor-free 5-field cron matcher (`cron_due`, `next_fire`,
   `parse_cron`). Stdlib only — no `croniter` dependency.
2. A schedule store backed by the `schedules` SQLite table
   (`add_schedule`, `list_schedules`, `remove_schedule`, `set_enabled`,
   `record_run`).
3. A background `ScheduleRunner` thread that polls the table and fires
   due rows via `subprocess.Popen`, isolated per schedule.

Cron semantics
--------------
Standard 5 fields: ``minute hour day-of-month month day-of-week``.
Supported tokens per field: ``*``, lists (``1,2``), ranges (``1-5``),
steps (``*/15`` and ``1-30/5``). Day-of-week is 0-6 with Sunday = 0
(``7`` is also accepted as Sunday).

When BOTH day-of-month and day-of-week are restricted (neither is ``*``)
this implementation requires **both** to match (logical AND). Note this
differs from classic Vixie cron, which ORs the two day fields. The AND
choice is deliberate, documented here, and pinned by a unit test.
"""
from __future__ import annotations

import shlex
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .events import EventLog, event_log_for_paths
from .paths import RuntimePaths
from .storage.db import init_db, transaction
from .time_utils import now_iso

# ---------------------------------------------------------------------------
# Cron parsing / matching
# ---------------------------------------------------------------------------

_FIELD_BOUNDS = (
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 6),   # day of week (0 = Sunday)
)
_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")


class CronError(ValueError):
    """Raised when a cron expression cannot be parsed."""


def _parse_field(token: str, low: int, high: int, *, name: str) -> set[int]:
    values: set[int] = set()
    for part in token.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"empty term in {name} field")
        step = 1
        if "/" in part:
            base, _, step_str = part.partition("/")
            try:
                step = int(step_str)
            except ValueError as exc:
                raise CronError(f"invalid step in {name}: {part!r}") from exc
            if step <= 0:
                raise CronError(f"step must be positive in {name}: {part!r}")
        else:
            base = part

        if base == "*":
            start, end = low, high
        elif "-" in base:
            start_str, _, end_str = base.partition("-")
            try:
                start, end = int(start_str), int(end_str)
            except ValueError as exc:
                raise CronError(f"invalid range in {name}: {part!r}") from exc
        else:
            try:
                start = end = int(base)
            except ValueError as exc:
                raise CronError(f"invalid value in {name}: {part!r}") from exc

        # Day-of-week tolerates 7 as Sunday; normalise into 0-6.
        if name == "day-of-week":
            if start == 7:
                start = 0
            if end == 7:
                end = 0
        if start > end:
            raise CronError(f"range start > end in {name}: {part!r}")
        if start < low or end > high:
            raise CronError(
                f"{name} value out of bounds [{low},{high}]: {part!r}"
            )
        values.update(range(start, end + 1, step))
    if not values:
        raise CronError(f"{name} field resolved to no values: {token!r}")
    return values


@dataclass(frozen=True)
class CronSpec:
    minute: frozenset[int]
    hour: frozenset[int]
    dom: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool

    def matches(self, when: datetime) -> bool:
        if when.minute not in self.minute:
            return False
        if when.hour not in self.hour:
            return False
        if when.month not in self.month:
            return False
        # Python weekday(): Monday=0..Sunday=6. Cron: Sunday=0..Saturday=6.
        cron_dow = (when.weekday() + 1) % 7
        dom_ok = when.day in self.dom
        dow_ok = cron_dow in self.dow
        if self.dom_restricted and self.dow_restricted:
            # Documented AND semantics (see module docstring).
            return dom_ok and dow_ok
        if self.dom_restricted:
            return dom_ok
        if self.dow_restricted:
            return dow_ok
        return True


def parse_cron(cron: str) -> CronSpec:
    """Parse a 5-field cron string into a `CronSpec`. Raises CronError."""
    if not isinstance(cron, str) or not cron.strip():
        raise CronError("cron expression must be a non-empty string")
    fields = cron.split()
    if len(fields) != 5:
        raise CronError(
            f"cron expression must have exactly 5 fields, got {len(fields)}: {cron!r}"
        )
    parsed: List[set[int]] = []
    for token, (low, high), fname in zip(fields, _FIELD_BOUNDS, _FIELD_NAMES):
        parsed.append(_parse_field(token, low, high, name=fname))
    return CronSpec(
        minute=frozenset(parsed[0]),
        hour=frozenset(parsed[1]),
        dom=frozenset(parsed[2]),
        month=frozenset(parsed[3]),
        dow=frozenset(parsed[4]),
        dom_restricted=fields[2].strip() != "*",
        dow_restricted=fields[4].strip() != "*",
    )


def is_valid_cron(cron: str) -> bool:
    try:
        parse_cron(cron)
    except CronError:
        return False
    return True


def cron_due(cron: str, now: datetime) -> bool:
    """True when `cron` matches the minute of `now`. Raises CronError on bad input."""
    return parse_cron(cron).matches(now)


def next_fire(cron: str, now: datetime, *, horizon_minutes: int = 366 * 24 * 60) -> Optional[datetime]:
    """Return the next datetime (minute precision, > now) at which `cron` fires.

    Scans minute-by-minute up to `horizon_minutes` (default ~1 year).
    Returns None if no match is found in the horizon (e.g. Feb 30).
    """
    spec = parse_cron(cron)
    # Start at the next whole minute strictly after `now`.
    candidate = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
    for _ in range(horizon_minutes):
        if spec.matches(candidate):
            return candidate
        candidate += timedelta(minutes=1)
    return None


# ---------------------------------------------------------------------------
# Schedule store
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Schedule:
    name: str
    cron: str
    action: str
    enabled: bool
    last_run: Optional[str]
    last_status: Optional[str]

    def as_dict(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "name": self.name,
            "cron": self.cron,
            "action": self.action,
            "enabled": self.enabled,
            "last_run": self.last_run,
            "last_status": self.last_status,
            "cron_valid": is_valid_cron(self.cron),
        }
        if data["cron_valid"]:
            ref = now or datetime.now(timezone.utc)
            nxt = next_fire(self.cron, ref)
            data["next_fire"] = nxt.strftime("%Y-%m-%dT%H:%M:%SZ") if nxt else None
        else:
            data["next_fire"] = None
        return data


def _row_to_schedule(row: sqlite3.Row) -> Schedule:
    return Schedule(
        name=row["name"],
        cron=row["cron"],
        action=row["action"],
        enabled=bool(row["enabled"]),
        last_run=row["last_run"],
        last_status=row["last_status"],
    )


def add_schedule(
    conn: sqlite3.Connection,
    *,
    name: str,
    cron: str,
    action: str,
    enabled: bool = True,
) -> Schedule:
    """Insert or replace a schedule. Validates the cron string first."""
    if not name or not name.strip():
        raise ValueError("schedule name must be non-empty")
    if not action or not action.strip():
        raise ValueError("schedule action must be non-empty")
    parse_cron(cron)  # raises CronError on bad input
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO schedules(name, cron, action, enabled, last_run, last_status)
            VALUES (?, ?, ?, ?, NULL, NULL)
            ON CONFLICT(name) DO UPDATE SET
              cron=excluded.cron,
              action=excluded.action,
              enabled=excluded.enabled;
            """,
            (name, cron, action, 1 if enabled else 0),
        )
    return get_schedule(conn, name)  # type: ignore[return-value]


def get_schedule(conn: sqlite3.Connection, name: str) -> Optional[Schedule]:
    row = conn.execute("SELECT * FROM schedules WHERE name = ?;", (name,)).fetchone()
    return _row_to_schedule(row) if row else None


def list_schedules(conn: sqlite3.Connection) -> List[Schedule]:
    rows = conn.execute("SELECT * FROM schedules ORDER BY name;").fetchall()
    return [_row_to_schedule(r) for r in rows]


def remove_schedule(conn: sqlite3.Connection, name: str) -> bool:
    with transaction(conn):
        cur = conn.execute("DELETE FROM schedules WHERE name = ?;", (name,))
    return cur.rowcount > 0


def set_enabled(conn: sqlite3.Connection, name: str, enabled: bool) -> bool:
    with transaction(conn):
        cur = conn.execute(
            "UPDATE schedules SET enabled = ? WHERE name = ?;",
            (1 if enabled else 0, name),
        )
    return cur.rowcount > 0


def record_run(
    conn: sqlite3.Connection,
    name: str,
    *,
    status: str,
    last_run: Optional[str] = None,
) -> None:
    stamp = last_run or now_iso()
    with transaction(conn):
        conn.execute(
            "UPDATE schedules SET last_run = ?, last_status = ? WHERE name = ?;",
            (stamp, status, name),
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def build_action_command(
    repo_root: Path, action: str, *, config_override: Optional[Path] = None
) -> List[str]:
    """Build the argv for an `agentic-os <action>` invocation.

    Mirrors `_spawn_dashboard_daemon`: invoke the package by module so we
    never depend on a bare `agentic-os` shim being on PATH. `shlex.split`
    keeps quoting intact; `shell=True` is never used.

    The scheduled child is a fresh process, so the parent's global
    `--config` override (issue #77) does not survive across the
    `subprocess.Popen` boundary. We re-pass it explicitly as a global flag
    (before the subcommand) so the child resolves the same runtime/SUT the
    daemon was launched with. When the caller passes no override we fall
    back to the active in-process override.
    """
    if config_override is None:
        from .config import get_active_config_override

        config_override = get_active_config_override()
    cmd = [sys.executable, "-m", "agentic_os", "--root", str(repo_root)]
    if config_override is not None:
        cmd.extend(["--config", str(config_override)])
    cmd.extend(shlex.split(action))
    return cmd


def fire_schedule(
    conn: sqlite3.Connection,
    events: EventLog,
    paths: RuntimePaths,
    schedule: Schedule,
    *,
    now: Optional[datetime] = None,
    config_override: Optional[Path] = None,
    popen=None,
) -> Dict[str, Any]:
    """Fire a single schedule via subprocess.Popen, isolated.

    Records last_run/last_status and emits a `schedule.fired` NDJSON event.
    Never raises — a launch failure is captured as last_status="error" so a
    sibling schedule is unaffected.

    `popen` is resolved at call time (defaulting to ``subprocess.Popen``) so
    tests can monkeypatch ``scheduler.subprocess.Popen`` and have it take
    effect on the runner's internal call path.
    """
    if popen is None:
        popen = subprocess.Popen
    when = now or datetime.now(timezone.utc)
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    cmd = build_action_command(
        paths.repo_root, schedule.action, config_override=config_override
    )
    status = "launched"
    pid: Optional[int] = None
    error: Optional[str] = None
    log_path = paths.subprocess_logs_dir / f"schedule-{schedule.name}.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "ab", buffering=0)
        try:
            proc = popen(
                cmd,
                stdout=log_file,
                stderr=log_file,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                cwd=str(paths.repo_root),
            )
            pid = getattr(proc, "pid", None)
        finally:
            log_file.close()
    except Exception as exc:  # launch failure must not kill the runner
        status = "error"
        error = str(exc)

    try:
        record_run(conn, schedule.name, status=status, last_run=stamp)
    except Exception:
        pass

    payload: Dict[str, Any] = {
        "name": schedule.name,
        "cron": schedule.cron,
        "action": schedule.action,
        "status": status,
        "pid": pid,
        "fired_at": stamp,
    }
    if error:
        payload["error"] = error
    try:
        events.write(
            "schedule.fired",
            actor="scheduler",
            severity="info" if status == "launched" else "warning",
            payload=payload,
        )
    except Exception:
        pass
    return payload


class ScheduleRunner:
    """Background thread that polls the schedules table and fires due rows.

    The thread owns its own SQLite connection (cross-thread sharing is
    illegal in sqlite3). Shutdown is prompt: `stop()` sets a threading
    Event the poll loop waits on instead of sleeping a fixed interval.
    """

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        poll_seconds: float = 60.0,
        config_override: Optional[Path] = None,
    ) -> None:
        self._paths = paths
        self._poll_seconds = poll_seconds
        self._config_override = config_override
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._events: Optional[EventLog] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="agentic-os-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        # The runner thread owns its SQLite connection; closing it here would
        # raise (cross-thread use is illegal). We only signal + join; the
        # thread closes its own connection in `_run`'s finally block. When the
        # thread was never started (synchronous `tick` use in tests) we close
        # from the caller thread, which IS the owning thread.
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            self._thread = None
        elif self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def _ensure_runtime(self) -> Tuple[sqlite3.Connection, EventLog]:
        if self._conn is None:
            self._conn = init_db(self._paths.db)
            self._events = event_log_for_paths(self._conn, self._paths)
        assert self._events is not None
        return self._conn, self._events

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.tick(now=datetime.now(timezone.utc))
                except Exception:
                    # A poll failure must never kill the runner thread.
                    pass
                # Wait returns True if stop was set — prompt shutdown.
                if self._stop.wait(self._poll_seconds):
                    break
        finally:
            # Close the connection from its owning thread.
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def tick(self, *, now: datetime) -> List[Dict[str, Any]]:
        """Evaluate all schedules at `now` and fire the due, enabled rows.

        Returns the list of fired payloads. Deduplicates by minute: a row
        whose `last_run` already falls in the current minute is skipped, so
        a sub-minute poll interval cannot double-fire it.
        """
        conn, events = self._ensure_runtime()
        fired: List[Dict[str, Any]] = []
        current_minute = now.strftime("%Y-%m-%dT%H:%M")
        for schedule in list_schedules(conn):
            if not schedule.enabled:
                continue
            try:
                if not cron_due(schedule.cron, now):
                    continue
            except CronError:
                # Invalid cron: skip silently here (doctor surfaces it).
                continue
            if schedule.last_run and schedule.last_run.startswith(current_minute):
                continue
            fired.append(
                fire_schedule(
                    conn,
                    events,
                    self._paths,
                    schedule,
                    now=now,
                    config_override=self._config_override,
                )
            )
        return fired


def run_now(
    conn: sqlite3.Connection,
    events: EventLog,
    paths: RuntimePaths,
    name: str,
    *,
    now: Optional[datetime] = None,
    config_override: Optional[Path] = None,
) -> Dict[str, Any]:
    """Ad-hoc trigger for a named schedule, bypassing cron/enabled checks."""
    schedule = get_schedule(conn, name)
    if schedule is None:
        raise ValueError(f"unknown schedule: {name}")
    return fire_schedule(
        conn, events, paths, schedule, now=now, config_override=config_override
    )


# ---------------------------------------------------------------------------
# Doctor integration helpers
# ---------------------------------------------------------------------------


def _parse_iso(stamp: Optional[str]) -> Optional[datetime]:
    if not stamp:
        return None
    text = stamp.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def audit_schedules(
    schedules: Sequence[Schedule], *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Doctor view over schedules: invalid cron = issue, stale = warning.

    A schedule is "stuck" when it is enabled, has fired at least once, and
    its `last_run` is older than 2x the nominal interval between fires
    (derived from `next_fire`). Invalid cron strings are hard issues.
    """
    ref = now or datetime.now(timezone.utc)
    issues: List[str] = []
    warnings: List[str] = []
    for sched in schedules:
        if not is_valid_cron(sched.cron):
            issues.append(f"schedule {sched.name!r} has invalid cron {sched.cron!r}")
            continue
        if not sched.enabled:
            continue
        last = _parse_iso(sched.last_run)
        if last is None:
            continue
        # Nominal interval = gap between the two upcoming fires.
        first = next_fire(sched.cron, last)
        if first is None:
            continue
        second = next_fire(sched.cron, first)
        if second is None:
            continue
        interval = second - first
        if interval.total_seconds() <= 0:
            continue
        if (ref - last) > (interval * 2):
            warnings.append(
                f"schedule {sched.name!r} last ran {sched.last_run} — "
                f"older than 2x its interval ({interval}); possibly stuck"
            )
    return {
        "count": len(schedules),
        "issues": issues,
        "warnings": warnings,
        "ok": not issues,
    }
