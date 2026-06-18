"""Issue #289 — per-project RAG memory (semantic recall of project history).

Every autonomous session starts blind to what prior sessions on the *same*
project learned. This module builds a per-project, denormalized SQLite FTS5
index (``memory_index``, migration v15 — zero new deps) over five canonical
history sources and answers ranked ``MATCH`` queries scoped to one project, so
the next session is fed compressed prior context instead of re-discovering it.

Per-source project scoping (the crux — "must not mix projects"):

- **summary**    — markdown files ``reports/session-summary-<sid>.md`` written
                   by :mod:`summaries`; scoped via ``autonomy_sessions.project_id``
                   for that session id. Unscoped (no session row) → skipped.
- **learning**   — ``learnings.project_id`` directly (backfilled to 'default').
- **transcript** — ``model_transcripts.invocation_id → model_invocations.task_id
                   → tasks.payload->>'work_item_id' → work_items.project_id``.
                   ``tasks`` has NO ``work_item_id`` column (only ``phase_id`` /
                   ``payload``), confirmed against schema.sql + the dashboard
                   reader that pulls ``payload["work_item_id"]``; so the link is
                   the JSON ``work_item_id`` key best-effort. Transcripts whose
                   chain yields no project are indexed under 'default' and emit a
                   ``memory.transcript_unscoped`` event — never silently dropped.
- **decision**   — the ``decisions`` table carries only ``phase_id`` (no work
                   item back-reference at all), so per-project scoping is not
                   possible. Decisions are indexed under 'default' best-effort
                   with a ``memory.decision_unscoped`` event.
- **bug**        — files ``bugs/BUG-*.md`` (no table). Front-matter is parsed for
                   a ``work_item_id`` back-ref → ``work_items.project_id``.
                   Unlinked bugs → 'default' + a ``memory.bug_unscoped`` event.

Every optional source is best-effort: one bad source must never abort the
whole rebuild. ``build_memory`` is idempotent — it deletes the project's rows
first, then re-inserts, so running twice yields identical contents.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from .storage.db import transaction

DEFAULT_PROJECT_ID = "default"

# Bug filenames look like BUG-NNN-*.md (triage_classifier convention).
_BUG_PREFIX = "BUG-"


# ---------------------------------------------------------------------------
# Front-matter (mirrors triage_classifier._parse_frontmatter — kept local so
# the memory build never imports the triage stack).
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> Dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    block = text[4:end]
    out: Dict[str, str] = {}
    for line in block.splitlines():
        m = re.match(r"^([a-zA-Z_]+)\s*:\s*(.+?)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _emit(events: Any, kind: str, payload: Dict[str, Any]) -> None:
    """Best-effort warning emit; a missing/raising event log never aborts build."""
    if events is None:
        return
    try:
        events.write(kind, severity="warning", payload=payload)
    except Exception:  # pragma: no cover - event emit must not break build
        pass


def _insert(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    source: str,
    source_id: str,
    ts: Optional[str],
    title: str,
    body: str,
) -> None:
    conn.execute(
        "INSERT INTO memory_index(project_id, source, source_id, ts, title, body) "
        "VALUES (?, ?, ?, ?, ?, ?);",
        (project_id, source, source_id, ts or "", title, body),
    )


# ---------------------------------------------------------------------------
# Per-source indexers. Each returns the number of rows it wrote for project_id
# and swallows its own errors (best-effort).
# ---------------------------------------------------------------------------


def _index_learnings(conn, project_id: str) -> int:
    try:
        rows = conn.execute(
            "SELECT id, subject, payload, observed_at FROM learnings WHERE project_id=?;",
            (project_id,),
        ).fetchall()
    except sqlite3.Error:
        return 0
    n = 0
    for r in rows:
        _insert(
            conn,
            project_id=project_id,
            source="learning",
            source_id=str(r["id"]),
            ts=r["observed_at"],
            title=str(r["subject"] or ""),
            body=f"{r['subject']} {r['payload']}",
        )
        n += 1
    return n


def _index_summaries(conn, paths, project_id: str) -> int:
    """Index session-summary markdown for sessions belonging to project_id."""
    try:
        rows = conn.execute(
            "SELECT id, started_at FROM autonomy_sessions WHERE project_id=?;",
            (project_id,),
        ).fetchall()
    except sqlite3.Error:
        return 0
    reports_dir = Path(paths.repo_root) / "reports"
    n = 0
    for r in rows:
        sid = r["id"]
        doc = reports_dir / f"session-summary-{sid}.md"
        try:
            text = doc.read_text(encoding="utf-8")
        except OSError:
            continue
        _insert(
            conn,
            project_id=project_id,
            source="summary",
            source_id=str(sid),
            ts=r["started_at"],
            title=f"session-summary-{sid}",
            body=text,
        )
        n += 1
    return n


def _index_transcripts(conn, events, project_id: str) -> int:
    """Index model_transcripts scoped to a project.

    Issue #339 — autonomous-pipeline rows now carry an explicit
    ``model_invocations.work_item_id`` FK to ``work_items``. The chain
    prefers it (``mi.work_item_id`` → ``work_items.project_id``); when
    NULL (legacy execution-path rows) it falls back to the historic
    chain ``mi.task_id → tasks.payload.$.work_item_id → work_items``.
    A transcript whose chain resolves to no project is indexed under
    'default' with a memory.transcript_unscoped event (never dropped).
    """
    try:
        rows = conn.execute(
            """
            SELECT mt.invocation_id AS inv,
                   mt.ord           AS ord,
                   mt.payload       AS payload,
                   mt.ts            AS ts,
                   COALESCE(wi_direct.project_id, wi_via_task.project_id) AS pid
            FROM model_transcripts mt
            LEFT JOIN model_invocations mi ON mi.id = mt.invocation_id
            LEFT JOIN work_items wi_direct ON wi_direct.id = mi.work_item_id
            LEFT JOIN tasks t              ON t.id = mi.task_id
            LEFT JOIN work_items wi_via_task ON wi_via_task.id = json_extract(t.payload, '$.work_item_id')
            ORDER BY mt.invocation_id, mt.ord;
            """
        ).fetchall()
    except sqlite3.Error:
        return 0
    n = 0
    for r in rows:
        pid = r["pid"]
        if pid is None:
            # Best-effort fallback: unscoped transcripts live under 'default'.
            _emit(events, "memory.transcript_unscoped", {"invocation_id": r["inv"]})
            pid = DEFAULT_PROJECT_ID
        if pid != project_id:
            continue
        _insert(
            conn,
            project_id=project_id,
            source="transcript",
            source_id=str(r["inv"]),
            ts=r["ts"],
            title=str(r["inv"]),
            body=str(r["payload"] or ""),
        )
        n += 1
    return n


def _index_decisions(conn, events, project_id: str) -> int:
    """Index decisions best-effort under 'default' (no work-item back-ref).

    The decisions table carries only phase_id, so it cannot be scoped to a
    project. Decisions are indexed under 'default'; a memory.decision_unscoped
    event flags the chosen path. Only the 'default' project receives them.
    """
    if project_id != DEFAULT_PROJECT_ID:
        return 0
    try:
        rows = conn.execute(
            "SELECT id, topic, rationale, consequences, decided_at FROM decisions;"
        ).fetchall()
    except sqlite3.Error:
        return 0
    n = 0
    for r in rows:
        _emit(events, "memory.decision_unscoped", {"decision_id": r["id"]})
        _insert(
            conn,
            project_id=DEFAULT_PROJECT_ID,
            source="decision",
            source_id=str(r["id"]),
            ts=r["decided_at"],
            title=str(r["topic"] or ""),
            body=f"{r['topic']} {r['rationale']} {r['consequences']}",
        )
        n += 1
    return n


def _index_bugs(conn, events, paths, project_id: str) -> int:
    """Index bugs/BUG-*.md files; scope via front-matter work_item_id back-ref.

    Unlinked bugs (no work_item_id, or one that resolves to no project) are
    indexed under 'default' with a memory.bug_unscoped event. Never fails the
    build; a missing bugs/ dir yields zero rows.
    """
    bugs_dir = Path(paths.repo_root) / "bugs"
    if not bugs_dir.exists():
        return 0
    n = 0
    try:
        entries = sorted(bugs_dir.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.is_file() or not entry.name.startswith(_BUG_PREFIX):
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = _parse_frontmatter(text)
        wid = meta.get("work_item_id")
        pid: Optional[str] = None
        if wid:
            try:
                row = conn.execute(
                    "SELECT project_id FROM work_items WHERE id=?;", (wid,)
                ).fetchone()
                if row is not None:
                    pid = row["project_id"]
            except sqlite3.Error:
                pid = None
        if pid is None:
            _emit(events, "memory.bug_unscoped", {"bug_id": entry.stem})
            pid = DEFAULT_PROJECT_ID
        if pid != project_id:
            continue
        _insert(
            conn,
            project_id=project_id,
            source="bug",
            source_id=entry.stem,
            ts=None,
            title=entry.stem,
            body=text,
        )
        n += 1
    return n


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_memory(conn: sqlite3.Connection, paths, *, project_id: str, events: Any = None) -> Dict[str, int]:
    """Rebuild ``memory_index`` for one project from canonical sources.

    Deletes the project's existing rows, then re-inserts all five sources
    scoped per this module's docstring. Idempotent: running twice yields
    identical contents. Returns per-source counts. Best-effort throughout — a
    single bad source must not abort the whole build, so each indexer swallows
    its own errors and the deletes/inserts run inside one transaction.
    """
    counts = {"learning": 0, "summary": 0, "transcript": 0, "decision": 0, "bug": 0}
    with transaction(conn):
        conn.execute("DELETE FROM memory_index WHERE project_id=?;", (project_id,))
        counts["learning"] = _index_learnings(conn, project_id)
        counts["summary"] = _index_summaries(conn, paths, project_id)
        counts["transcript"] = _index_transcripts(conn, events, project_id)
        counts["decision"] = _index_decisions(conn, events, project_id)
        counts["bug"] = _index_bugs(conn, events, paths, project_id)
    return counts


# FTS5 special characters / operators we must neutralize so arbitrary user
# text cannot raise a syntax error.
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _sanitize_match(text: str) -> Optional[str]:
    """Turn arbitrary user text into a safe FTS5 MATCH expression.

    Strategy: extract alphanumeric word tokens (dropping every operator and
    punctuation char FTS5 treats specially: AND/OR/NEAR/"/(/)/* etc.), wrap each
    in double quotes (phrase form) so even a bare token can never be parsed as
    an operator, and join with ``OR``. OR (rather than implicit AND) is the
    right recall semantics here: the query is often a full prompt while indexed
    bodies are short snippets, so requiring *every* term would miss relevant
    rows — bm25 still ranks the best matches first. Returns ``None`` when the
    input has no usable tokens, so the caller can short-circuit to an empty
    result instead of issuing a degenerate query.
    """
    tokens = _TOKEN_RE.findall(text or "")
    if not tokens:
        return None
    # Quote each token; doubling any stray quote is unnecessary since the regex
    # already excludes them, but stay defensive.
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def query_memory(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    text: str,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Return ranked prior-context snippets for ``project_id`` matching ``text``.

    Scoped to one project (``WHERE project_id=?``) so results never mix across
    projects. Ranked by ``bm25``. The MATCH expression is sanitized so hostile
    input (``a AND ("`` etc.) never raises an FTS5 syntax error — it returns an
    empty list when no usable terms remain.
    """
    match = _sanitize_match(text)
    if match is None:
        return []
    lim = max(1, min(int(limit), 100))
    try:
        rows = conn.execute(
            "SELECT source, source_id, ts, title, "
            "snippet(memory_index, 5, '[', ']', '…', 16) AS snippet, "
            "bm25(memory_index) AS score "
            "FROM memory_index "
            "WHERE project_id=? AND memory_index MATCH ? "
            "ORDER BY bm25(memory_index) LIMIT ?;",
            (project_id, match, lim),
        ).fetchall()
    except sqlite3.Error:
        # Defensive: any residual FTS5 syntax issue degrades to no results
        # rather than propagating — memory is advisory, never load-bearing.
        return []
    return [
        {
            "source": r["source"],
            "source_id": r["source_id"],
            "ts": r["ts"],
            "title": r["title"],
            "snippet": r["snippet"],
            "score": float(r["score"]),
        }
        for r in rows
    ]
