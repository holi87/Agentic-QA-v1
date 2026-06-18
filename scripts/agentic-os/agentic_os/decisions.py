"""Decision trail helpers (issue #247).

The `decisions` table records why a gate flipped — operator overrides and
autonomous decisions alike. `decided_by` keeps its constrained model-role
semantics (opus/sonnet/codex/operator/script); `actor` carries the full
identity (planner-autopilot, triager-autopilot, operator) so the
Verifications view can separate the autonomous trail from human overrides.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from .ids import ulid
from .paths import RuntimePaths
from .storage.db import init_db, transaction
from .time_utils import now_iso

# Maps a full actor identity to the constrained decided_by model role.
_ACTOR_TO_DECIDED_BY = {
    "planner-autopilot": "opus",
    "implementer-autopilot": "sonnet",
    "reviewer-autopilot": "codex",
    "triager-autopilot": "script",
    "operator": "operator",
}


def _decided_by_for(actor: str, *, fallback: str = "script") -> str:
    return _ACTOR_TO_DECIDED_BY.get(actor, fallback)


def record_decision(
    conn: sqlite3.Connection,
    *,
    phase_id: str,
    topic: str,
    actor: str,
    rationale: str,
    consequences: str = "",
    decided_by: Optional[str] = None,
) -> str:
    """Insert one decision row. Returns the new decision id."""
    resolved_decided_by = decided_by or _decided_by_for(actor)
    decision_id = ulid()
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO decisions(
                id, phase_id, topic, decided_by, rationale, consequences,
                decided_at, actor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                decision_id,
                phase_id,
                topic,
                resolved_decided_by,
                rationale,
                consequences,
                now_iso(),
                actor,
            ),
        )
    return decision_id


def record_autopilot_decision(
    paths: RuntimePaths,
    *,
    phase_id: str,
    topic: str,
    actor: str,
    rationale: str,
    consequences: str = "",
) -> Optional[str]:
    """Best-effort decision write that opens its own short-lived connection.

    For call sites (planner coverage architect, triager batch) that have
    `paths` but not a live DB handle. Never raises — a failed decision
    write must not break the autonomy loop.
    """
    try:
        conn = init_db(paths.db)
    except Exception:
        return None
    try:
        return record_decision(
            conn,
            phase_id=phase_id,
            topic=topic,
            actor=actor,
            rationale=rationale,
            consequences=consequences,
        )
    except Exception:
        return None
    finally:
        conn.close()


def get_decision(conn: sqlite3.Connection, decision_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single decision row by id (no recency truncation)."""
    row = conn.execute(
        """
        SELECT id, phase_id, topic, decided_by, actor, rationale,
               consequences, decided_at, reversed_by
          FROM decisions WHERE id = ?;
        """,
        (decision_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "phase_id": row["phase_id"],
        "topic": row["topic"],
        "decided_by": row["decided_by"],
        "actor": row["actor"] or row["decided_by"],
        "rationale": row["rationale"],
        "consequences": row["consequences"],
        "decided_at": row["decided_at"],
        "reversed_by": row["reversed_by"],
    }


def fetch_decisions(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    actor: Optional[str] = None,
    before: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return recent decision rows (newest first).

    `actor` accepts an exact value or a trailing-glob (e.g. `*-autopilot`).
    `before` paginates by decision id (returns rows with id < before).
    """
    where: List[str] = []
    params: List[Any] = []
    if actor:
        lead = actor.startswith("*")
        trail = actor.endswith("*")
        core = actor.strip("*")
        if lead and trail:
            where.append("actor LIKE ?")
            params.append("%" + core + "%")
        elif lead:
            where.append("actor LIKE ?")
            params.append("%" + core)
        elif trail:
            where.append("actor LIKE ?")
            params.append(core + "%")
        else:
            where.append("actor = ?")
            params.append(actor)
    if before:
        where.append("id < ?")
        params.append(before)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(max(1, min(int(limit), 500)))
    rows = conn.execute(
        f"""
        SELECT id, phase_id, topic, decided_by, actor, rationale,
               consequences, decided_at, reversed_by
          FROM decisions
          {clause}
         ORDER BY decided_at DESC, id DESC
         LIMIT ?;
        """,
        params,
    ).fetchall()
    return [
        {
            "id": r["id"],
            "phase_id": r["phase_id"],
            "topic": r["topic"],
            "decided_by": r["decided_by"],
            "actor": r["actor"] or r["decided_by"],
            "rationale": r["rationale"],
            "consequences": r["consequences"],
            "decided_at": r["decided_at"],
            "reversed_by": r["reversed_by"],
        }
        for r in rows
    ]
