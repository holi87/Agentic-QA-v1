"""Issue #269 — durable autonomy session index + replay support.

The live autonomy session lives in-process (`autonomy._SessionState`); this
module is the post-hoc audit record the `/sessions` history, replay and
compare views read. Session rows are written at start and finalised at stop;
counts come from the in-memory events_log because the events NDJSON carries no
session id and cannot be grouped after the fact.

Replay reuses the existing NDJSON: the detail view filters
`/api/events/history?from=<started_at>&to=<finished_at>` — no new event format.
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .time_utils import now_iso

_DEFAULT_RETENTION_DAYS = 30


def record_session_start(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    started_at: str,
    mode: str,
    max_minutes: Optional[int],
    primary_actor: str = "autonomy",
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO autonomy_sessions(
            id, started_at, status, mode, max_minutes, primary_actor
        ) VALUES (?, ?, 'running', ?, ?, ?);
        """,
        (session_id, started_at, mode, max_minutes, primary_actor),
    )


def finalize_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    status: str,
    finished_at: Optional[str] = None,
    work_items_processed: int = 0,
    blocks: int = 0,
    failures: int = 0,
) -> None:
    conn.execute(
        """
        UPDATE autonomy_sessions
           SET status=?, finished_at=?, work_items_processed=?, blocks=?, failures=?
         WHERE id=?;
        """,
        (status, finished_at or now_iso(), work_items_processed, blocks, failures, session_id),
    )


def classify_event(entry: Dict[str, Any]) -> Tuple[Optional[str], bool, bool]:
    """Classify one session-log entry.

    Returns ``(work_item_id_or_None, is_block, is_failure)``. Block and
    failure are mutually exclusive, matching the original aggregation. Shared
    by `counts_from_events_log` and the autonomy session's running counters so
    the two never drift (issue #265).
    """
    step = entry.get("step") or ""
    work_item: Optional[str] = None
    if ":" in step:
        _, _, candidate = step.partition(":")
        if candidate.startswith("WI-") or candidate.startswith("wi-"):
            work_item = candidate
    is_block = "blocked" in step or "awaiting_operator_decision" in step
    is_failure = not is_block and not entry.get("ok", True)
    return work_item, is_block, is_failure


def counts_from_events_log(events_log: List[Dict[str, Any]]) -> Dict[str, int]:
    """Derive work_items_processed / blocks / failures from the session log."""
    work_items = set()
    blocks = 0
    failures = 0
    for entry in events_log or []:
        work_item, is_block, is_failure = classify_event(entry)
        if work_item:
            work_items.add(work_item)
        if is_block:
            blocks += 1
        elif is_failure:
            failures += 1
    return {
        "work_items_processed": len(work_items),
        "blocks": blocks,
        "failures": failures,
    }


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(zip(row.keys(), row)) if hasattr(row, "keys") else dict(row)
    return d


def list_sessions(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    offset: int = 0,
    mode: Optional[str] = None,
    status: Optional[str] = None,
    actor: Optional[str] = None,
) -> List[Dict[str, Any]]:
    where: List[str] = []
    params: List[Any] = []
    if mode:
        where.append("s.mode=?")
        params.append(mode)
    if status:
        where.append("s.status=?")
        params.append(status)
    if actor:
        where.append("s.primary_actor=?")
        params.append(actor)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.extend([max(1, min(int(limit), 200)), max(0, int(offset))])
    rows = conn.execute(
        f"""
        SELECT s.*, b.label AS bookmark
          FROM autonomy_sessions s
          LEFT JOIN session_bookmarks b ON b.session_id = s.id
          {clause}
         ORDER BY s.started_at DESC
         LIMIT ? OFFSET ?;
        """,
        params,
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_session(conn: sqlite3.Connection, session_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT s.*, b.label AS bookmark
          FROM autonomy_sessions s
          LEFT JOIN session_bookmarks b ON b.session_id = s.id
         WHERE s.id=?;
        """,
        (session_id,),
    ).fetchone()
    return _row_to_dict(row) if row else None


def set_bookmark(conn: sqlite3.Connection, session_id: str, label: str) -> bool:
    """Tag a session. Returns False when the session does not exist."""
    exists = conn.execute(
        "SELECT 1 FROM autonomy_sessions WHERE id=?;", (session_id,)
    ).fetchone()
    if not exists:
        return False
    if label:
        conn.execute(
            """
            INSERT INTO session_bookmarks(session_id, label, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET label=excluded.label;
            """,
            (session_id, label, now_iso()),
        )
    else:
        conn.execute("DELETE FROM session_bookmarks WHERE session_id=?;", (session_id,))
    return True


def compare_sessions(conn: sqlite3.Connection, a: str, b: str) -> Dict[str, Any]:
    sa, sb = get_session(conn, a), get_session(conn, b)
    fields: Dict[str, Any] = {}
    for key in ("work_items_processed", "blocks", "failures", "max_minutes"):
        va = (sa or {}).get(key) or 0
        vb = (sb or {}).get(key) or 0
        fields[key] = {"a": va, "b": vb, "delta": vb - va}
    return {"a": sa, "b": sb, "fields": fields}


def sweep_retention(
    paths: Any,
    *,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
) -> Dict[str, Any]:
    """Archive NDJSON event files older than the retention window.

    DB rows are kept for the index; only the bulky NDJSON is moved to
    `<runtime>/archive/<yyyy-mm>/`. Best-effort and idempotent.
    """
    moved: List[str] = []
    events_dir = getattr(paths, "events_dir", None)
    runtime_root = getattr(paths, "runtime_root", None)
    if events_dir is None or runtime_root is None or not Path(events_dir).exists():
        return {"moved": moved, "retention_days": retention_days}
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max(0, retention_days))
    for path in sorted(Path(events_dir).glob("*.ndjson")):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        bucket = mtime.strftime("%Y-%m")
        dest_dir = Path(runtime_root) / "archive" / bucket
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        try:
            shutil.move(str(path), str(dest))
            moved.append(str(dest))
        except OSError:
            continue
    return {"moved": moved, "retention_days": retention_days}
