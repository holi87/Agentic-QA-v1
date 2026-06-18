"""Queue prioritization policies for the autonomous work-item loop (issue #274).

The autonomy loop historically picked queued work items in arrival order
(``created_at ASC, id ASC``). This module makes the ordering policy
configurable via ``autonomy.queue_policy`` while guaranteeing that the
common case — equal-priority, dependency-free items — degrades to the exact
historical FIFO order.

Every policy shares the same final tie-break: ``created_at ASC, id ASC``.
That single invariant is what makes ``HYBRID`` (the default) behaviour
preserving when no priorities or dependency edges are configured.

Policies:
  * ``FIFO``        — pure arrival order. The reference order.
  * ``PRIORITY``    — P0 first … P3 last, then arrival order.
  * ``DEPENDENCY``  — topological: a child is never returned before its
                      parent reaches ``done``; among selectable items,
                      arrival order.
  * ``BUDGET_FAIR`` — smallest estimated token cost first, then arrival
                      order. Estimation is best-effort (see
                      ``estimate_tokens``); equal/unknown estimates fall
                      back to arrival order so it stays FIFO-equivalent
                      when nothing is known.
  * ``HYBRID``      — priority desc, then dependency-eligibility, then
                      budget-fair, then arrival order.
"""
from __future__ import annotations

import sqlite3
from enum import Enum
from typing import Dict, List, Optional, Set

# Lower number = higher precedence. P0 is the most urgent.
_PRIORITY_RANK: Dict[str, int] = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_DEFAULT_PRIORITY_RANK = _PRIORITY_RANK["P2"]


class QueuePolicy(Enum):
    FIFO = "fifo"
    PRIORITY = "priority"
    DEPENDENCY = "dependency"
    BUDGET_FAIR = "budget_fair"
    HYBRID = "hybrid"


DEFAULT_QUEUE_POLICY = QueuePolicy.HYBRID


def coerce_policy(value: object) -> QueuePolicy:
    """Map a config string (or QueuePolicy) to a QueuePolicy.

    Unknown / missing values fall back to the default (HYBRID). The lookup is
    case-insensitive so ``"HYBRID"`` and ``"hybrid"`` both work.
    """
    if isinstance(value, QueuePolicy):
        return value
    if isinstance(value, str):
        try:
            return QueuePolicy(value.strip().lower())
        except ValueError:
            return DEFAULT_QUEUE_POLICY
    return DEFAULT_QUEUE_POLICY


class _Item:
    """A queued work item plus the fields the policies sort on."""

    __slots__ = ("id", "priority", "created_at", "tokens", "deps_satisfied")

    def __init__(
        self,
        *,
        id: str,
        priority: str,
        created_at: str,
        tokens: int,
        deps_satisfied: bool,
    ) -> None:
        self.id = id
        self.priority = priority
        self.created_at = created_at
        self.tokens = tokens
        self.deps_satisfied = deps_satisfied

    @property
    def priority_rank(self) -> int:
        return _PRIORITY_RANK.get(self.priority, _DEFAULT_PRIORITY_RANK)

    # The canonical FIFO tie-break, reused by every policy.
    @property
    def fifo_key(self) -> tuple:
        return (self.created_at, self.id)


def _load_queued(conn: sqlite3.Connection) -> List[_Item]:
    """Return queued work items in the canonical FIFO order.

    The ORDER BY here is the single source of truth for FIFO: it matches the
    historical inline selection (``created_at ASC, id ASC``). Every policy
    re-sorts this list with a key that ends in ``fifo_key``, so a stable sort
    preserves this order on ties.
    """
    rows = conn.execute(
        """
        SELECT id, priority, created_at
          FROM work_items
         WHERE status = 'queued'
         ORDER BY created_at ASC, id ASC;
        """
    ).fetchall()
    if not rows:
        return []
    tokens = _estimate_tokens_bulk(conn, [row["id"] for row in rows])
    deps = _unsatisfied_parents(conn, [row["id"] for row in rows])
    items: List[_Item] = []
    for row in rows:
        wid = row["id"]
        items.append(
            _Item(
                id=wid,
                priority=row["priority"],
                created_at=row["created_at"],
                tokens=tokens.get(wid, 0),
                deps_satisfied=wid not in deps,
            )
        )
    return items


def _unsatisfied_parents(conn: sqlite3.Connection, ids: List[str]) -> Set[str]:
    """Return the subset of ``ids`` that have at least one parent not `done`.

    A child is dependency-satisfied only when every parent edge points at a
    work item whose status is ``done``. Missing parents (edge to a deleted
    row — should not happen under the FK cascade) are treated as unsatisfied
    to stay safe.
    """
    if not ids:
        return set()
    blocked: Set[str] = set()
    rows = conn.execute(
        """
        SELECT d.child_id   AS child_id,
               p.status      AS parent_status
          FROM work_item_deps AS d
          LEFT JOIN work_items AS p ON p.id = d.parent_id;
        """
    ).fetchall()
    id_set = set(ids)
    for row in rows:
        child = row["child_id"]
        if child not in id_set:
            continue
        if row["parent_status"] != "done":
            blocked.add(child)
    return blocked


def _estimate_tokens_bulk(conn: sqlite3.Connection, ids: List[str]) -> Dict[str, int]:
    """Best-effort estimated token cost per work item.

    Uses recorded ``model_invocations`` token totals (tokens_in + tokens_out)
    grouped by the work item's task lineage where available. When no signal
    exists the estimate is 0 for every item, which makes BUDGET_FAIR collapse
    to pure FIFO (the tie-break) — exactly the behaviour-preserving default.

    The join is intentionally lenient: ``model_invocations`` is keyed by
    ``task_id`` (scheduler tasks), not work-item id, so most autonomous runs
    have no rows yet. This returns whatever it can and never raises.
    """
    blank = {wid: 0 for wid in ids}
    try:
        rows = conn.execute(
            """
            SELECT task_id AS wid,
                   COALESCE(SUM(tokens_in + tokens_out), 0) AS total
              FROM model_invocations
             WHERE task_id IS NOT NULL
             GROUP BY task_id;
            """
        ).fetchall()
    except sqlite3.Error:
        return blank
    for row in rows:
        wid = row["wid"]
        if wid in blank:
            blank[wid] = int(row["total"] or 0)
    return blank


def _order(items: List[_Item], policy: QueuePolicy) -> List[_Item]:
    """Return ``items`` ordered per ``policy``.

    ``items`` arrives in canonical FIFO order. Python's ``sorted`` is stable,
    so any key whose final component is ``fifo_key`` (or that omits a
    discriminator entirely) preserves FIFO among equal elements.
    """
    if policy is QueuePolicy.FIFO:
        # Already FIFO-ordered; return a copy to avoid surprising callers.
        return list(items)

    if policy is QueuePolicy.PRIORITY:
        return sorted(items, key=lambda it: (it.priority_rank, it.fifo_key))

    if policy is QueuePolicy.DEPENDENCY:
        # Selectable (deps satisfied) first, then FIFO. Blocked children sort
        # last so they are never *returned* as next until unblocked.
        return sorted(items, key=lambda it: (0 if it.deps_satisfied else 1, it.fifo_key))

    if policy is QueuePolicy.BUDGET_FAIR:
        return sorted(items, key=lambda it: (it.tokens, it.fifo_key))

    # HYBRID: priority desc, then dependency-eligibility, then budget-fair,
    # then FIFO. With no priorities/deps/budget signal every leading key is
    # constant and the result is the FIFO order.
    return sorted(
        items,
        key=lambda it: (
            it.priority_rank,
            0 if it.deps_satisfied else 1,
            it.tokens,
            it.fifo_key,
        ),
    )


def ordered_work_items(conn: sqlite3.Connection, *, policy: QueuePolicy) -> List[str]:
    """Return queued work-item ids ordered by ``policy``.

    For DEPENDENCY/HYBRID, ids whose parent is not yet ``done`` are pushed to
    the back (and excluded from being the *next* item) but still listed so the
    caller can see the full queue. Use :func:`next_work_item` for the single
    runnable head.
    """
    items = _load_queued(conn)
    return [it.id for it in _order(items, policy)]


def order_pending(
    conn: sqlite3.Connection,
    items: List[Dict[str, object]],
    *,
    policy: QueuePolicy,
) -> List[Dict[str, object]]:
    """Reorder an in-memory list of work-item dicts using ``policy``.

    The autonomy loop processes items across several statuses (``queued``,
    ``analyzing``, ``planned``, ``implementing`` …), not just ``queued``, so it
    cannot route through :func:`next_work_item` (which scopes to ``queued``).
    Instead it hands its already-filtered ``pending`` list here and gets it
    back reordered by the configured policy.

    Each dict must carry ``id``, ``priority`` and ``created_at`` (the columns
    ``list_work_items`` emits). Dependency-eligibility and token estimates are
    looked up from ``conn`` so DEPENDENCY/HYBRID still honour ``work_item_deps``.

    The ordering keys are identical to :func:`_order`, so with no priorities,
    dependency edges, or budget signal the result is the canonical FIFO order
    (``created_at ASC, id ASC``) regardless of the input list's order — the
    behaviour-preserving default for HYBRID.

    Unlike :func:`next_work_item`, dependency-blocked children are NOT dropped:
    they sort to the back (so an unblocked sibling is processed first) but
    remain in the list. The loop's own pipeline gating decides what to do once
    an item is reached; this function only orders.
    """
    if not items:
        return []
    ids = [str(it.get("id")) for it in items if it.get("id")]
    tokens = _estimate_tokens_bulk(conn, ids)
    blocked = _unsatisfied_parents(conn, ids)
    wrapped: List[tuple] = []
    for original in items:
        wid = str(original.get("id"))
        wrapped.append(
            (
                _Item(
                    id=wid,
                    priority=str(original.get("priority") or "P2"),
                    created_at=str(original.get("created_at") or ""),
                    tokens=tokens.get(wid, 0),
                    deps_satisfied=wid not in blocked,
                ),
                original,
            )
        )
    # Pre-sort into canonical FIFO order so the policy keys (which tie-break on
    # fifo_key, and for FIFO assume the input is already FIFO-ordered) behave
    # identically to the DB-backed path where `_load_queued` emits FIFO order.
    projection = sorted((w[0] for w in wrapped), key=lambda it: it.fifo_key)
    ordered_items = _order(projection, policy)
    by_id = {w[0].id: w[1] for w in wrapped}
    return [by_id[it.id] for it in ordered_items]


def next_work_item(conn: sqlite3.Connection, *, policy: QueuePolicy) -> Optional[str]:
    """Return the id of the next work item to process, or None when idle.

    For DEPENDENCY and HYBRID a child whose parent is not ``done`` is never
    returned. For FIFO/PRIORITY/BUDGET_FAIR dependency edges are ignored (the
    issue scopes dependency enforcement to the dependency-aware policies).
    """
    items = _load_queued(conn)
    if not items:
        return None
    ordered = _order(items, policy)
    if policy in (QueuePolicy.DEPENDENCY, QueuePolicy.HYBRID):
        for it in ordered:
            if it.deps_satisfied:
                return it.id
        return None
    return ordered[0].id
