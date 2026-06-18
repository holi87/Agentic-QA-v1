"""Issue #274 — queue prioritization policy ordering.

Covers the behaviour-preserving guarantees of ``agentic_os.queue``:

  * FIFO-equivalence: equal-priority, dependency-free queued items come back
    in identical order under FIFO and HYBRID (the default).
  * PRIORITY: a P0 queued *after* a P3 is selected first.
  * DEPENDENCY: a child whose parent is not yet ``done`` is never returned as
    the next item under DEPENDENCY / HYBRID until the parent reaches ``done``.

These exercise the public entry points (``next_work_item`` /
``ordered_work_items``) against a real on-disk sqlite runtime so the schema
migrations (incl. v12 ``work_item_deps``) are honoured.
"""
from __future__ import annotations

from pathlib import Path

from agentic_os import queue
from agentic_os.storage.db import init_db


def _insert_work_item(
    conn,
    wid: str,
    *,
    priority: str = "P2",
    status: str = "queued",
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO work_items(id, title, status, spec_path, sut_root,
                               priority, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (wid, f"item {wid}", status, f"specs/{wid}.md", ".", priority,
         created_at, created_at),
    )
    conn.commit()


def _link(conn, *, parent: str, child: str, created_at: str) -> None:
    conn.execute(
        "INSERT INTO work_item_deps(parent_id, child_id, kind, created_at) "
        "VALUES (?, ?, 'blocks', ?);",
        (parent, child, created_at),
    )
    conn.commit()


def test_queue_fifo_and_hybrid_match_for_equal_priority_dep_free_items(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        # Three equal-priority, dependency-free items at distinct create times.
        _insert_work_item(conn, "wi_a", created_at="2026-01-01T00:00:00.000Z")
        _insert_work_item(conn, "wi_b", created_at="2026-01-02T00:00:00.000Z")
        _insert_work_item(conn, "wi_c", created_at="2026-01-03T00:00:00.000Z")

        fifo = queue.ordered_work_items(conn, policy=queue.QueuePolicy.FIFO)
        hybrid = queue.ordered_work_items(conn, policy=queue.QueuePolicy.HYBRID)

        assert fifo == ["wi_a", "wi_b", "wi_c"]
        assert hybrid == fifo
        # And the next item agrees too.
        assert queue.next_work_item(conn, policy=queue.QueuePolicy.FIFO) == "wi_a"
        assert queue.next_work_item(conn, policy=queue.QueuePolicy.HYBRID) == "wi_a"
    finally:
        conn.close()


def test_queue_priority_promotes_later_p0_over_earlier_p3(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        _insert_work_item(conn, "wi_low", priority="P3", created_at="2026-01-01T00:00:00.000Z")
        _insert_work_item(conn, "wi_high", priority="P0", created_at="2026-01-02T00:00:00.000Z")

        assert queue.next_work_item(conn, policy=queue.QueuePolicy.PRIORITY) == "wi_high"
        assert queue.next_work_item(conn, policy=queue.QueuePolicy.HYBRID) == "wi_high"
        # FIFO ignores priority — earliest wins.
        assert queue.next_work_item(conn, policy=queue.QueuePolicy.FIFO) == "wi_low"
    finally:
        conn.close()


def test_queue_dependency_blocks_child_until_parent_done(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        # Parent created first, child second; child depends on parent.
        _insert_work_item(conn, "wi_parent", created_at="2026-01-01T00:00:00.000Z")
        _insert_work_item(conn, "wi_child", created_at="2026-01-02T00:00:00.000Z")
        _link(conn, parent="wi_parent", child="wi_child", created_at="2026-01-02T00:00:00.000Z")

        # Parent still queued => parent selectable, child blocked.
        assert queue.next_work_item(conn, policy=queue.QueuePolicy.DEPENDENCY) == "wi_parent"
        assert queue.next_work_item(conn, policy=queue.QueuePolicy.HYBRID) == "wi_parent"

        # Drain the parent (mark done) — but to surface the child as the only
        # queued item, also move parent off the queue.
        conn.execute("UPDATE work_items SET status='done' WHERE id='wi_parent';")
        conn.commit()

        # Now the only queued item is the child, and its parent is done.
        assert queue.next_work_item(conn, policy=queue.QueuePolicy.DEPENDENCY) == "wi_child"
        assert queue.next_work_item(conn, policy=queue.QueuePolicy.HYBRID) == "wi_child"
    finally:
        conn.close()


def test_order_pending_reorders_in_memory_dicts_across_statuses(tmp_path: Path) -> None:
    """`order_pending` (the autonomy-loop entry point) orders dicts spanning
    multiple statuses — not just `queued` — which `next_work_item` cannot."""
    conn = init_db(tmp_path / "state.db")
    try:
        # The loop hands an already-filtered list spanning pipeline statuses.
        items = [
            {"id": "wi_c", "priority": "P2", "created_at": "2026-01-03T00:00:00.000Z", "status": "queued"},
            {"id": "wi_a", "priority": "P2", "created_at": "2026-01-01T00:00:00.000Z", "status": "analyzing"},
            {"id": "wi_b", "priority": "P2", "created_at": "2026-01-02T00:00:00.000Z", "status": "planned"},
        ]
        # FIFO/HYBRID with equal priorities => canonical created_at ASC order,
        # regardless of the input ordering or per-item status.
        fifo = [d["id"] for d in queue.order_pending(conn, items, policy=queue.QueuePolicy.FIFO)]
        hybrid = [d["id"] for d in queue.order_pending(conn, items, policy=queue.QueuePolicy.HYBRID)]
        assert fifo == ["wi_a", "wi_b", "wi_c"]
        assert hybrid == fifo
        # The returned objects are the original dicts (status preserved).
        first = queue.order_pending(conn, items, policy=queue.QueuePolicy.FIFO)[0]
        assert first["status"] == "analyzing"
    finally:
        conn.close()


def test_order_pending_priority_promotes_p0_among_mixed_statuses(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        items = [
            {"id": "wi_low", "priority": "P3", "created_at": "2026-01-01T00:00:00.000Z", "status": "analyzing"},
            {"id": "wi_high", "priority": "P0", "created_at": "2026-01-05T00:00:00.000Z", "status": "implementing"},
            {"id": "wi_mid", "priority": "P2", "created_at": "2026-01-03T00:00:00.000Z", "status": "queued"},
        ]
        ordered = [d["id"] for d in queue.order_pending(conn, items, policy=queue.QueuePolicy.PRIORITY)]
        assert ordered[0] == "wi_high"
        assert ordered == ["wi_high", "wi_mid", "wi_low"]
    finally:
        conn.close()


def test_queue_dependency_returns_none_when_only_child_is_blocked(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        # Parent is mid-pipeline (analyzing, not done), child queued + blocked.
        _insert_work_item(conn, "wi_parent2", status="analyzing", created_at="2026-01-01T00:00:00.000Z")
        _insert_work_item(conn, "wi_child2", created_at="2026-01-02T00:00:00.000Z")
        _link(conn, parent="wi_parent2", child="wi_child2", created_at="2026-01-02T00:00:00.000Z")

        # Only the child is queued; it is blocked => no runnable next item.
        assert queue.next_work_item(conn, policy=queue.QueuePolicy.DEPENDENCY) is None
        assert queue.next_work_item(conn, policy=queue.QueuePolicy.HYBRID) is None
    finally:
        conn.close()
