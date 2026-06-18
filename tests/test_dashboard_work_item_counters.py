"""Issue #191 — /api/status must expose work-item queue counters.

Background
----------
The home Runtime card on the dashboard reads `/api/status` and renders
``tasks.queued`` / ``tasks.running`` / ``tasks.failed`` as the operator
queue.  Those numbers, however, come from ``fetch_task_summary`` which
aggregates the **internal scheduler** ``tasks`` table — not the operator
``work_items`` table that ``/api/tasks`` lists.  The two are unrelated
in practice: operators create work items, the scheduler table stays
empty, so the Runtime card shows ``Queued = 0`` while ``/api/tasks``
returns multiple ``queued`` work items.

This regression test seeds ``queued`` work items directly, hits
``/api/status``, and asserts the new ``work_items`` summary block matches
the underlying ``/api/tasks`` queue.
"""
from __future__ import annotations

import threading
from pathlib import Path

from agentic_os.events import EventLog
from agentic_os.server import make_server
from agentic_os.storage.db import connect
from agentic_os.work_items import create_work_item_from_payload

from test_dashboard_server import _free_port, _runtime  # type: ignore[import-not-found]
from test_dashboard_action_gating import _get_json  # type: ignore[import-not-found]


def _seed_queued_work_item(paths, title: str) -> str:
    conn = connect(paths.db)
    try:
        detail = create_work_item_from_payload(
            conn,
            paths,
            EventLog(conn, paths),
            {
                "title": title,
                "priority": "P2",
                "business_goal": "Issue #191 — runtime counter parity.",
                "expected_behavior": "/api/status reflects the queued items.",
            },
        )
    finally:
        conn.close()
    return detail["work_item"]["id"]


def _start(paths):
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{port}", srv, thread


def _stop(srv, thread):
    srv._shutdown_requested = True  # type: ignore[attr-defined]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def test_status_exposes_work_item_counters(tmp_path: Path) -> None:
    paths = _runtime(tmp_path, enable_write=True)
    a = _seed_queued_work_item(paths, "Smoke item A")
    b = _seed_queued_work_item(paths, "Smoke item B")

    base, srv, thread = _start(paths)
    try:
        status = _get_json(base + "/api/status")
        tasks_payload = _get_json(base + "/api/tasks")

        # New field — work-item-level counters, the operator queue view.
        assert "work_items" in status, (
            "GET /api/status must expose work-item status counters "
            "(issue #191); got: " + repr(list(status.keys()))
        )
        wi = status["work_items"]
        assert isinstance(wi, dict), wi

        # The home Runtime card must NOT report `Queued = 0` while the
        # underlying queue still has items.
        queued_in_queue = sum(
            1 for t in tasks_payload["tasks"] if t.get("status") == "queued"
        )
        assert queued_in_queue == 2, tasks_payload
        assert wi.get("queued") == queued_in_queue, (
            "work-item queued counter must match /api/tasks queue; "
            f"got status={wi!r} tasks={tasks_payload!r}"
        )

        # All declared work-item statuses must be present so the UI never
        # crashes on `undefined`.
        for required_key in (
            "queued",
            "analyzing",
            "planned",
            "implementing",
            "reviewing",
            "running",
            "blocked",
            "done",
            "failed",
        ):
            assert required_key in wi, (required_key, wi)

        # `total` is convenient for the UI and asserted against /api/tasks.
        assert wi.get("total") == len(tasks_payload["tasks"]), (wi, tasks_payload)

        # Both seeded IDs visible — sanity that we hit the same datastore.
        ids = {t["id"] for t in tasks_payload["tasks"]}
        assert a in ids and b in ids, ids
    finally:
        _stop(srv, thread)


def test_status_work_item_counters_track_status_transitions(tmp_path: Path) -> None:
    """Move one item to ``analyzing`` and confirm counters re-aggregate."""
    from agentic_os.work_items import update_work_item_status

    paths = _runtime(tmp_path, enable_write=True)
    wid_q = _seed_queued_work_item(paths, "Stays queued")
    wid_a = _seed_queued_work_item(paths, "Moves to analyzing")

    conn = connect(paths.db)
    try:
        update_work_item_status(
            conn,
            EventLog(conn, paths),
            work_item_id=wid_a,
            status="analyzing",
        )
    finally:
        conn.close()

    base, srv, thread = _start(paths)
    try:
        status = _get_json(base + "/api/status")
        wi = status["work_items"]
        assert wi["queued"] == 1, wi
        assert wi["analyzing"] == 1, wi
        assert wi["total"] == 2, wi
    finally:
        _stop(srv, thread)
