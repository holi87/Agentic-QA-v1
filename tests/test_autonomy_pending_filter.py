"""Regression: the autonomy loop must select pending work by ``status``.

Pre-existing bug (commit d382ea6): ``_run_loop`` filtered the queue by
``w.get("phase")``, but ``list_work_items`` rows carry ``status`` (there is no
``phase`` key) and the value set used non-statuses like ``"planning"`` /
``"pending"``. The filter therefore matched nothing, ``pending`` was always
empty, and the loop's ``for wi in pending`` execution branch was dead — queued
work items were never picked up by a running session.

These tests pin the corrected selection against the real ``status`` values
produced by ``work_items`` (see ``VALID_WORK_ITEM_STATUSES``).
"""
from __future__ import annotations

from agentic_os.autonomy import _select_pending


def _wi(status: str, wid: str = "wi") -> dict:
    # Mirrors a list_work_items row: status-bearing, no 'phase' key.
    return {"id": wid, "title": "t", "status": status}


def test_freshly_queued_item_is_selected():
    items = [_wi("queued", "a")]
    assert [w["id"] for w in _select_pending(items)] == ["a"]


def test_early_pipeline_statuses_are_selected():
    statuses = ["queued", "analyzing", "planned", "implementing"]
    items = [_wi(s, s) for s in statuses]
    assert {w["id"] for w in _select_pending(items)} == set(statuses)


def test_terminal_and_waiting_statuses_are_excluded():
    for status in ("done", "failed", "blocked", "draft"):
        assert _select_pending([_wi(status)]) == [], status


def test_selection_keys_off_status_not_phase():
    # An item carrying a stale 'phase' hint but a terminal status must NOT be
    # selected — status is authoritative, the old 'phase' lookup was the bug.
    item = {"id": "x", "title": "t", "status": "done", "phase": "implementing"}
    assert _select_pending([item]) == []
