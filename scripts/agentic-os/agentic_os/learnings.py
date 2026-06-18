"""Issue #273 — cross-run learnings store.

Without cross-run memory every autonomous session starts blind. This module
is a small SQLite-backed table of *advisory* hints distilled from history:

- ``flaky``            — a scenario alternates pass/fail; planner deprioritises.
- ``skill_failure``    — a skill keeps getting REJECTed; reason cluster.
- ``provider_quality`` — a (role, provider) pair's historical success rate;
                          the provider router prefers the better one.
- ``coverage_gap``     — a recurring candidate-gap pattern the architect saw.

Learnings are HINTS, never authority. Every reviewer/triager output is still
graded by the configured gates (#251 envelope, #258 AST guard, #233
quantitative). A wrong learning produces a wrong hint; the gate catches the
wrong action. Read sites are limited to the planner (quarantine flaky) and the
provider router (per operator decision 2026-05-27); the triager short-circuit
and the ``triage_pattern`` kind were deliberately dropped.

Recency over frequency: there is one row per ``(kind, subject)``. Re-observing
a subject resets ``observed_at`` to now and ``weight`` back to ``1.0`` rather
than accumulating a count. ``decay_learnings`` recomputes ``weight`` from
``observed_at`` nightly via the scheduler and prunes rows that fade below the
floor.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .runtime.tuning import LEARNING_DECAY_TAU_DAYS, LEARNING_MIN_WEIGHT
from .storage.db import transaction
from .time_utils import now_iso

VALID_KINDS = ("flaky", "skill_failure", "provider_quality", "coverage_gap")

_TS_FORMATS = ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(zip(row.keys(), row)) if hasattr(row, "keys") else dict(row)
    if "payload" in d and isinstance(d["payload"], str):
        try:
            d["payload"] = json.loads(d["payload"])
        except (ValueError, TypeError):
            d["payload"] = {}
    return d


def decayed_weight(
    observed_at: Optional[str],
    *,
    now: Optional[datetime] = None,
    tau_days: float = LEARNING_DECAY_TAU_DAYS,
) -> float:
    """``exp(-age_days / tau_days)`` clamped to ``[0, 1]``.

    ``tau_days`` is an e-folding time, not a half-life (weight is ~0.37 at
    age == tau). An unparseable / future ``observed_at`` yields ``1.0``.
    """
    seen = _parse_ts(observed_at)
    if seen is None or tau_days <= 0:
        return 1.0
    ref = now or datetime.now(timezone.utc)
    age_days = max(0.0, (ref - seen).total_seconds() / 86400.0)
    return round(math.exp(-age_days / tau_days), 6)


def record_learning(
    conn: sqlite3.Connection,
    *,
    kind: str,
    subject: str,
    payload: Dict[str, Any],
    actor: str,
    observed_at: Optional[str] = None,
) -> None:
    """Observe a learning. Upserts one row per ``(kind, subject)``.

    Re-observing resets ``observed_at`` to now and ``weight`` to ``1.0`` so
    recent signals dominate stale ones. Raises ``ValueError`` on an unknown
    kind; callers at write sites should wrap this so a bad hint never breaks
    the host flow.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown learning kind: {kind!r}")
    if not subject:
        raise ValueError("learning subject must be non-empty")
    stamp = observed_at or now_iso()
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO learnings(kind, subject, payload, observed_at, weight, actor)
            VALUES (?, ?, ?, ?, 1.0, ?)
            ON CONFLICT(kind, subject) DO UPDATE SET
              payload=excluded.payload,
              observed_at=excluded.observed_at,
              weight=1.0,
              actor=excluded.actor;
            """,
            (kind, subject, json.dumps(payload, sort_keys=True), stamp, actor),
        )


def get_learning(conn: sqlite3.Connection, learning_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM learnings WHERE id=?;", (learning_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_learnings(
    conn: sqlite3.Connection,
    *,
    kind: Optional[str] = None,
    subject: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    where: List[str] = []
    params: List[Any] = []
    if kind:
        where.append("kind=?")
        params.append(kind)
    if subject:
        where.append("subject=?")
        params.append(subject)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.extend([max(1, min(int(limit), 200)), max(0, int(offset))])
    rows = conn.execute(
        f"SELECT * FROM learnings{clause} ORDER BY weight DESC, observed_at DESC, id "
        "LIMIT ? OFFSET ?;",
        params,
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def forget_learning(conn: sqlite3.Connection, learning_id: int) -> bool:
    """Operator override: drop a single learning. False when it did not exist."""
    with transaction(conn):
        cur = conn.execute("DELETE FROM learnings WHERE id=?;", (learning_id,))
    return cur.rowcount > 0


def decay_learnings(
    conn: sqlite3.Connection,
    *,
    now: Optional[datetime] = None,
    tau_days: float = LEARNING_DECAY_TAU_DAYS,
    min_weight: float = LEARNING_MIN_WEIGHT,
) -> Dict[str, int]:
    """Recompute every ``weight`` from ``observed_at`` then prune the faded.

    Fired nightly by the scheduler (``agentic-os learnings decay``). Returns
    ``{"recomputed": N, "pruned": M}``.
    """
    ref = now or datetime.now(timezone.utc)
    rows = conn.execute("SELECT id, observed_at FROM learnings;").fetchall()
    recomputed = 0
    pruned = 0
    with transaction(conn):
        for r in rows:
            lid = r["id"] if hasattr(r, "keys") else r[0]
            seen = r["observed_at"] if hasattr(r, "keys") else r[1]
            weight = decayed_weight(seen, now=ref, tau_days=tau_days)
            if weight < min_weight:
                conn.execute("DELETE FROM learnings WHERE id=?;", (lid,))
                pruned += 1
            else:
                conn.execute(
                    "UPDATE learnings SET weight=? WHERE id=?;", (weight, lid)
                )
                recomputed += 1
    return {"recomputed": recomputed, "pruned": pruned}


# ---------------------------------------------------------------------------
# Typed read helpers (consumed by the planner + provider router). These are
# pure reads; the caller emits the `learning.consulted` audit event since the
# ranking/filter functions live below the event-log layer.
# ---------------------------------------------------------------------------


def flaky_subjects(
    conn: sqlite3.Connection, *, min_weight: float = LEARNING_MIN_WEIGHT
) -> List[str]:
    """Subjects (``feature_uri::scenario``) of live ``flaky`` learnings.

    The planner quarantines these — schedules them apart from the green path.
    Stored ``weight`` is used directly (the nightly decay keeps it fresh).
    """
    rows = conn.execute(
        "SELECT subject FROM learnings WHERE kind='flaky' AND weight >= ? "
        "ORDER BY weight DESC, subject;",
        (min_weight,),
    ).fetchall()
    return [r["subject"] if hasattr(r, "keys") else r[0] for r in rows]


def coverage_gap_subjects(
    conn: sqlite3.Connection, *, min_weight: float = LEARNING_MIN_WEIGHT, limit: int = 50
) -> List[Dict[str, Any]]:
    """Live ``coverage_gap`` learnings as ``{subject, payload}`` dicts.

    Subject convention is ``sut_key::category``. The prompt injector turns
    these into "watch this gap" hints for the planner/implementer.
    """
    rows = conn.execute(
        "SELECT subject, payload FROM learnings WHERE kind='coverage_gap' "
        "AND weight >= ? ORDER BY weight DESC, subject LIMIT ?;",
        (min_weight, max(1, min(int(limit), 200))),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        subject = r["subject"] if hasattr(r, "keys") else r[0]
        raw_payload = r["payload"] if hasattr(r, "keys") else r[1]
        payload: Dict[str, Any] = {}
        if isinstance(raw_payload, str):
            try:
                payload = json.loads(raw_payload)
            except (ValueError, TypeError):
                payload = {}
        out.append({"subject": subject, "payload": payload})
    return out


def skill_failure_subjects(
    conn: sqlite3.Connection, *, min_weight: float = LEARNING_MIN_WEIGHT, limit: int = 50
) -> List[Dict[str, Any]]:
    """Live ``skill_failure`` learnings as ``{subject, payload}`` dicts.

    Subject convention is ``reviewer::scope``; payload clusters the reject
    reason. Surfaced to the implementer so it pre-empts the recurring reject.
    """
    rows = conn.execute(
        "SELECT subject, payload FROM learnings WHERE kind='skill_failure' "
        "AND weight >= ? ORDER BY weight DESC, subject LIMIT ?;",
        (min_weight, max(1, min(int(limit), 200))),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        subject = r["subject"] if hasattr(r, "keys") else r[0]
        raw_payload = r["payload"] if hasattr(r, "keys") else r[1]
        payload: Dict[str, Any] = {}
        if isinstance(raw_payload, str):
            try:
                payload = json.loads(raw_payload)
            except (ValueError, TypeError):
                payload = {}
        out.append({"subject": subject, "payload": payload})
    return out


def provider_quality_scores(conn: sqlite3.Connection, *, role: str) -> Dict[str, float]:
    """Map ``provider -> weight`` for ``provider_quality`` learnings of ``role``.

    Subject convention is ``role::provider``. Higher weight = more recently
    confirmed good. The router uses this to break ties between live providers.
    """
    rows = conn.execute(
        "SELECT subject, weight FROM learnings WHERE kind='provider_quality' "
        "AND subject LIKE ?;",
        (f"{role}::%",),
    ).fetchall()
    scores: Dict[str, float] = {}
    for r in rows:
        subject = r["subject"] if hasattr(r, "keys") else r[0]
        weight = r["weight"] if hasattr(r, "keys") else r[1]
        _, _, provider = subject.partition("::")
        if provider:
            scores[provider] = float(weight)
    return scores
