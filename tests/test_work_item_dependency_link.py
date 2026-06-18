"""Issue #274 — `task link` records a work_item_deps edge.

Exercises both the library entry point (``link_work_items``) and the CLI
subcommand (``agentic-os task link <child> --needs <parent>``) and asserts the
edge lands in the ``work_item_deps`` table.
"""
from __future__ import annotations

from pathlib import Path

from agentic_os.cli import cmd_task
from agentic_os.events import EventLog
from agentic_os.paths import runtime_paths
from agentic_os.storage.db import init_db
from agentic_os.work_items import (
    create_work_item_from_payload,
    link_work_items,
    list_work_item_deps,
)


def _seed(conn, paths, title: str) -> str:
    detail = create_work_item_from_payload(
        conn,
        paths,
        EventLog(conn, paths),
        {
            "title": title,
            "priority": "P2",
            "business_goal": "Issue #274 — dependency link.",
            "expected_behavior": "edge persists.",
        },
    )
    return detail["work_item"]["id"]


def test_link_work_items_inserts_dependency_edge(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths.ensure()
    conn = init_db(paths.db)
    try:
        parent = _seed(conn, paths, "parent task")
        child = _seed(conn, paths, "child task")
        edge = link_work_items(
            conn, EventLog(conn, paths), parent_id=parent, child_id=child
        )
        assert edge == {"parent_id": parent, "child_id": child, "kind": "blocks"}

        deps = list_work_item_deps(conn)
        assert any(
            d["parent_id"] == parent and d["child_id"] == child for d in deps
        )

        # Idempotent re-link does not duplicate the edge.
        link_work_items(conn, EventLog(conn, paths), parent_id=parent, child_id=child)
        assert len(list_work_item_deps(conn)) == 1
    finally:
        conn.close()


def test_task_link_cli_subcommand_persists_edge(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths.ensure()
    conn = init_db(paths.db)
    try:
        parent = _seed(conn, paths, "cli parent")
        child = _seed(conn, paths, "cli child")
    finally:
        conn.close()

    rc = cmd_task(
        tmp_path,
        ["link", child, "--needs", parent],
        json_output=True,
    )
    assert rc == 0

    conn = init_db(paths.db)
    try:
        deps = list_work_item_deps(conn)
        assert any(
            d["parent_id"] == parent and d["child_id"] == child for d in deps
        )
    finally:
        conn.close()
