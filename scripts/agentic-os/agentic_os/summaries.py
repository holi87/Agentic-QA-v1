"""Issue #272 — session summary artifact (PR-ready handoff doc).

When an autonomous session ends the operator should read ONE document and
know whether to merge / re-run / escalate. `render_session_summary` produces
deterministic markdown from the durable record; `write_session_summary`
persists it to `reports/session-summary-<id>.md` (idempotent overwrite).

Scoping: `model_invocations` carry `session_id` and are read directly. The
other tables (decisions, test_results, bugs, work_items) have no session id,
so they are scoped by the session's `[started_at, finished_at]` time window —
the same approach issue #269 uses for replay. Comparisons parse timestamps to
datetimes because the codebase mixes millisecond (`time_utils.now_iso`) and
second (`autonomy._now_iso`) ISO precision, which is not safely comparable
lexically.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .sessions import get_session

_TS_FORMATS = ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ")


def summary_relpath(session_id: str) -> str:
    """Repo-relative path of the summary doc for ``session_id``."""
    return f"reports/session-summary-{session_id}.md"


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _in_window(
    ts: Optional[str], start: Optional[datetime], end: Optional[datetime]
) -> bool:
    dt = _parse_ts(ts)
    if dt is None:
        return False
    if start is not None and dt < start:
        return False
    if end is not None and dt > end:
        return False
    return True


def _derive_outcome(status: str, blocks: int, failures: int) -> str:
    """Map session status + counts to ok|partial|blocked.

    `blocked` wins when work stalled awaiting an operator and nothing failed;
    `partial` covers any failure (or a `failed` session status); otherwise
    `ok`. A single source so the doc and any future caller never diverge.
    """
    if status == "failed":
        return "partial"
    if blocks > 0 and failures == 0:
        return "blocked"
    if failures > 0:
        return "partial"
    return "ok"


def build_session_summary(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    processed_work_items: Optional[Iterable[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Gather the summary data for ``session_id``.

    Returns ``None`` when the session row does not exist. ``processed_work_items``
    (the autopilot's in-memory list) is authoritative when supplied; otherwise
    the work items are derived from `work_items.updated_at` inside the session
    window.
    """
    sess = get_session(conn, session_id)
    if sess is None:
        return None

    start = _parse_ts(sess.get("started_at"))
    end = _parse_ts(sess.get("finished_at"))
    blocks = int(sess.get("blocks") or 0)
    failures = int(sess.get("failures") or 0)
    work_items_processed = int(sess.get("work_items_processed") or 0)

    duration_minutes: Optional[int] = None
    if start is not None and end is not None:
        duration_minutes = max(0, round((end - start).total_seconds() / 60))

    # Test results — scoped by created_at window, deterministic ordering.
    test_rows = conn.execute(
        "SELECT status, created_at FROM test_results ORDER BY created_at, id;"
    ).fetchall()
    tests_total = tests_passed = tests_failed = 0
    for r in test_rows:
        if not _in_window(r["created_at"], start, end):
            continue
        tests_total += 1
        if r["status"] == "passed":
            tests_passed += 1
        elif r["status"] == "failed":
            tests_failed += 1

    # Bugs — first seen inside the window are "filed this session"; bugs only
    # re-observed (last_seen in window, first_seen before) are "already known".
    bug_rows = conn.execute(
        "SELECT id, severity, status, scenario_tag, first_seen, last_seen "
        "FROM bugs ORDER BY id;"
    ).fetchall()
    bugs_filed: List[Dict[str, Any]] = []
    bugs_known: List[Dict[str, Any]] = []
    for r in bug_rows:
        filed = _in_window(r["first_seen"], start, end)
        reobserved = _in_window(r["last_seen"], start, end)
        if filed:
            bugs_filed.append(dict(r))
        elif reobserved:
            bugs_known.append(dict(r))

    # Provider activity — model_invocations carry session_id directly.
    provider_rows = conn.execute(
        "SELECT model_role AS role, "
        "COUNT(*) AS invocations, "
        "COALESCE(SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)),0) AS tokens, "
        "COALESCE(SUM(COALESCE(cost_usd,0)),0) AS cost "
        "FROM model_invocations WHERE session_id=? "
        "GROUP BY model_role ORDER BY model_role;",
        (session_id,),
    ).fetchall()
    providers = [
        {
            "role": r["role"],
            "invocations": int(r["invocations"]),
            "tokens": int(r["tokens"]),
            "cost_usd": round(float(r["cost"]), 2),
        }
        for r in provider_rows
    ]
    budget_tokens = sum(p["tokens"] for p in providers)
    budget_usd = round(sum(p["cost_usd"] for p in providers), 2)

    # Autonomous decisions — scoped by decided_at window.
    decision_rows = conn.execute(
        "SELECT id, actor, decided_by, topic, decided_at "
        "FROM decisions ORDER BY decided_at, id;"
    ).fetchall()
    decisions = [
        {
            "id": r["id"],
            "actor": r["actor"] or r["decided_by"],
            "topic": r["topic"],
        }
        for r in decision_rows
        if _in_window(r["decided_at"], start, end)
    ]

    # Per work item — explicit list when the autopilot supplies it, else
    # derive from work_items touched inside the window.
    if processed_work_items is not None:
        wanted = sorted(set(processed_work_items))
        wi_rows = [
            dict(r)
            for wid in wanted
            for r in conn.execute(
                "SELECT id, title, status, priority FROM work_items WHERE id=?;",
                (wid,),
            ).fetchall()
        ]
    else:
        all_wi = conn.execute(
            "SELECT id, title, status, priority, updated_at "
            "FROM work_items ORDER BY id;"
        ).fetchall()
        wi_rows = [
            {"id": r["id"], "title": r["title"], "status": r["status"], "priority": r["priority"]}
            for r in all_wi
            if _in_window(r["updated_at"], start, end)
        ]

    open_work_items = [
        w for w in wi_rows if w.get("status") in ("blocked", "failed")
    ]

    return {
        "session_id": session_id,
        "mode": sess.get("mode"),
        "status": sess.get("status"),
        "started_at": sess.get("started_at"),
        "ended_at": sess.get("finished_at"),
        "duration_minutes": duration_minutes,
        "outcome": _derive_outcome(sess.get("status") or "", blocks, failures),
        "budget_consumed": {"tokens": budget_tokens, "usd": budget_usd},
        "headline": {
            "work_items_processed": work_items_processed,
            "blocks": blocks,
            "failures": failures,
            "tests_total": tests_total,
            "tests_passed": tests_passed,
            "tests_failed": tests_failed,
            "bugs_filed": len(bugs_filed),
            "bugs_known": len(bugs_known),
        },
        "work_items": wi_rows,
        "providers": providers,
        "decisions": decisions,
        "bugs_filed": bugs_filed,
        "bugs_known": bugs_known,
        "open_work_items": open_work_items,
    }


def _md(data: Dict[str, Any]) -> str:
    h = data["headline"]
    lines: List[str] = []
    lines.append("---")
    lines.append(f"session_id: {data['session_id']}")
    lines.append(f"mode: {data['mode']}")
    lines.append(f"status: {data['status']}")
    lines.append(f"started_at: {data['started_at']}")
    lines.append(f"ended_at: {data['ended_at']}")
    lines.append(f"duration_minutes: {data['duration_minutes']}")
    lines.append(f"outcome: {data['outcome']}")
    bc = data["budget_consumed"]
    lines.append(f"budget_consumed: {{ tokens: {bc['tokens']}, usd: {bc['usd']:.2f} }}")
    lines.append("---")
    lines.append("")
    lines.append(f"# Session summary — {data['session_id']}")
    lines.append("")

    lines.append("## Headline")
    lines.append(
        f"- Work items processed: {h['work_items_processed']} "
        f"({h['blocks']} blocked, {h['failures']} failed)"
    )
    lines.append(
        f"- Tests run: {h['tests_total']} → {h['tests_passed']} pass, {h['tests_failed']} fail"
    )
    lines.append(
        f"- Bugs filed: {h['bugs_filed']} (already known: {h['bugs_known']})"
    )
    lines.append("")

    lines.append("## Per work item")
    if data["work_items"]:
        for w in data["work_items"]:
            title = w.get("title") or ""
            suffix = f" — {title}" if title else ""
            lines.append(f"- {w['id']} ({w.get('status')}){suffix}")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Provider activity")
    if data["providers"]:
        lines.append("| Role | Invocations | Tokens | USD |")
        lines.append("|---|---|---|---|")
        for p in data["providers"]:
            lines.append(
                f"| {p['role']} | {p['invocations']} | {p['tokens']} | {p['cost_usd']:.2f} |"
            )
    else:
        lines.append("- (no model invocations)")
    lines.append("")

    lines.append("## Decisions taken (autonomous)")
    if data["decisions"]:
        for d in data["decisions"]:
            lines.append(f"- {d['id']} — {d['actor']} — {d['topic']}")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Open items")
    open_lines: List[str] = []
    for b in data["bugs_filed"]:
        if b.get("status") == "open":
            open_lines.append(
                f"- {b['id']} ({b.get('severity')}, {b.get('scenario_tag')}) — needs operator review."
            )
    for w in data["open_work_items"]:
        open_lines.append(f"- {w['id']} — {w.get('status')}.")
    if open_lines:
        lines.extend(open_lines)
    else:
        lines.append("- (none)")
    lines.append("")

    return "\n".join(lines) + "\n"


def render_session_summary(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    processed_work_items: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """Return deterministic markdown for ``session_id`` (None if no such session)."""
    data = build_session_summary(
        conn, session_id, processed_work_items=processed_work_items
    )
    if data is None:
        return None
    return _md(data)


def write_session_summary(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    reports_dir: Path,
    processed_work_items: Optional[Iterable[str]] = None,
) -> Optional[Path]:
    """Write the summary to ``reports/session-summary-<id>.md``. Idempotent.

    Returns the written path, or ``None`` when the session row is missing.
    """
    markdown = render_session_summary(
        conn, session_id, processed_work_items=processed_work_items
    )
    if markdown is None:
        return None
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    dest = reports_dir / f"session-summary-{session_id}.md"
    dest.write_text(markdown, encoding="utf-8")
    return dest
