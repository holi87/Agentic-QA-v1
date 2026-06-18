"""Issue #192 — dashboard must surface candidate debt on `done` tasks.

Background
----------
A work item can reach ``status=done`` (final-gate approved) while the
plan still contains ``needs_operator_decision`` candidates that were
never approved/rejected. The dashboard rendered such tasks as plain
"done" — operators never saw the nine pending test decisions.

Per #192 acceptance criteria, the API must:

- expose a ``candidate_debt`` block on the listing (``/api/tasks``)
  and the detail (``/api/tasks/<id>``) endpoints, derived from
  ``TEST-PLAN.json``;
- include a ``done_with_pending_decisions`` flag so the UI can render a
  warning chip when ``status == "done"`` and
  ``candidate_debt.needs_operator_decision > 0``.

The state machine itself is *not* changed here — the task can still
reach ``done`` (workflows/gates own that contract). The dashboard
surfaces the debt; AC #4 (final-gate semantics review) is tracked as
follow-up because ``workflows.py``/``gates.py`` are intentionally
out-of-scope for this branch.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from agentic_os.atomic_io import atomic_write_json
from agentic_os.events import EventLog
from agentic_os.server import make_server
from agentic_os.storage.db import connect
from agentic_os.work_items import create_work_item_from_payload

from test_dashboard_server import _free_port, _runtime  # type: ignore[import-not-found]
from test_dashboard_action_gating import _get_json  # type: ignore[import-not-found]


def _seed_work_item(paths, title: str = "Done with pending tests") -> str:
    conn = connect(paths.db)
    try:
        detail = create_work_item_from_payload(
            conn,
            paths,
            EventLog(conn, paths),
            {
                "title": title,
                "priority": "P2",
                "business_goal": "Issue #192 — candidate debt surfacing.",
                "expected_behavior": "Dashboard shows debt on done tasks.",
            },
        )
    finally:
        conn.close()
    return detail["work_item"]["id"]


def _force_status(paths, wid: str, status: str) -> None:
    """Set the work-item status directly. We bypass `update_work_item_status`
    because it enforces gate prerequisites; this test seeds the broken
    end state we are reporting on (status=done while plan still has
    pending decisions)."""
    ts = "2026-05-25T20:06:50+00:00"
    conn = connect(paths.db)
    try:
        conn.execute(
            "UPDATE work_items SET status=?, updated_at=? WHERE id=?;",
            (status, ts, wid),
        )
        conn.commit()
    finally:
        conn.close()


def _write_plan(
    paths,
    wid: str,
    *,
    needs_decision: int,
    generate_now: int,
    not_testable: int = 0,
    blocked: int = 0,
) -> None:
    plan_dir = paths.runtime_root / "plans" / wid
    plan_dir.mkdir(parents=True, exist_ok=True)
    items = []
    counter = 0
    for _ in range(generate_now):
        counter += 1
        items.append({
            "candidate_id": f"c{counter}",
            "title": f"Approved {counter}",
            "decision": "generate_now",
            "test_type": "api",
            "expected_assertion": "ok",
        })
    for _ in range(needs_decision):
        counter += 1
        items.append({
            "candidate_id": f"c{counter}",
            "title": f"Pending {counter}",
            "decision": "needs_operator_decision",
            "test_type": "api",
            "expected_assertion": "ok",
        })
    for _ in range(not_testable):
        counter += 1
        items.append({
            "candidate_id": f"c{counter}",
            "title": f"NotTestable {counter}",
            "decision": "not_testable",
            "test_type": "api",
            "expected_assertion": "ok",
        })
    for _ in range(blocked):
        counter += 1
        items.append({
            "candidate_id": f"c{counter}",
            "title": f"Blocked {counter}",
            "decision": "blocked_missing_docs",
            "test_type": "api",
            "expected_assertion": "ok",
        })
    summary = {
        "total": len(items),
        "generate_now": generate_now,
        "needs_operator_decision": needs_decision,
        "not_testable": not_testable,
        "blocked_missing_docs": blocked,
    }
    atomic_write_json(plan_dir / "TEST-PLAN.json", {"items": items, "summary": summary})


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


def test_done_task_with_pending_decisions_is_flagged_on_list(tmp_path: Path) -> None:
    """The scenario from the bug report: 1 approved, 9 needs_operator_decision,
    task forced to ``done``. The list endpoint must surface debt + warning."""
    paths = _runtime(tmp_path, enable_write=True)
    wid = _seed_work_item(paths)
    _write_plan(paths, wid, needs_decision=9, generate_now=1)
    _force_status(paths, wid, "done")

    base, srv, thread = _start(paths)
    try:
        payload = _get_json(base + "/api/tasks")
        row = next(t for t in payload["tasks"] if t["id"] == wid)

        # Candidate debt block is the new contract from #192.
        assert "candidate_debt" in row, (
            "/api/tasks rows must include a `candidate_debt` block per "
            "issue #192 acceptance criteria; got: " + repr(list(row.keys()))
        )
        debt = row["candidate_debt"]
        assert debt.get("total") == 10, debt
        assert debt.get("generate_now") == 1, debt
        assert debt.get("needs_operator_decision") == 9, debt

        # The UI consumes this flag to render a warning chip on `done`
        # tasks that still have undecided candidates.
        assert row.get("done_with_pending_decisions") is True, row
    finally:
        _stop(srv, thread)


def test_done_task_without_pending_decisions_is_not_flagged(tmp_path: Path) -> None:
    """Negative: a done task with every candidate decided must NOT carry
    the warning flag (so the UI doesn't false-positive)."""
    paths = _runtime(tmp_path, enable_write=True)
    wid = _seed_work_item(paths, "Clean done task")
    _write_plan(paths, wid, needs_decision=0, generate_now=2, not_testable=1)
    _force_status(paths, wid, "done")

    base, srv, thread = _start(paths)
    try:
        payload = _get_json(base + "/api/tasks")
        row = next(t for t in payload["tasks"] if t["id"] == wid)
        debt = row.get("candidate_debt") or {}
        assert debt.get("needs_operator_decision", 0) == 0, debt
        assert row.get("done_with_pending_decisions") is False, row
    finally:
        _stop(srv, thread)


def test_task_without_plan_has_zeroed_debt(tmp_path: Path) -> None:
    """A task that has never been planned must still expose the field
    (zeroed) so the UI never sees `undefined`."""
    paths = _runtime(tmp_path, enable_write=True)
    wid = _seed_work_item(paths, "Never planned")

    base, srv, thread = _start(paths)
    try:
        payload = _get_json(base + "/api/tasks")
        row = next(t for t in payload["tasks"] if t["id"] == wid)
        assert "candidate_debt" in row, row
        debt = row["candidate_debt"]
        assert debt.get("total", 0) == 0, debt
        assert debt.get("needs_operator_decision", 0) == 0, debt
        assert row.get("done_with_pending_decisions") is False, row
    finally:
        _stop(srv, thread)


def test_done_task_detail_includes_candidate_debt(tmp_path: Path) -> None:
    """The task detail endpoint also surfaces the debt block — the
    detail page is where the prominent plan summary lives (AC #1)."""
    paths = _runtime(tmp_path, enable_write=True)
    wid = _seed_work_item(paths)
    _write_plan(paths, wid, needs_decision=9, generate_now=1)
    _force_status(paths, wid, "done")

    base, srv, thread = _start(paths)
    try:
        detail = _get_json(base + "/api/tasks/" + wid)
        assert "candidate_debt" in detail, detail
        debt = detail["candidate_debt"]
        assert debt["total"] == 10, debt
        assert debt["needs_operator_decision"] == 9, debt
        assert detail["work_item"]["status"] == "done", detail["work_item"]
        assert detail.get("done_with_pending_decisions") is True, detail
    finally:
        _stop(srv, thread)
