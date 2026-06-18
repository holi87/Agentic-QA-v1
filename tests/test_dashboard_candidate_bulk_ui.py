"""Browser regression for candidate review bulk UI (issue #138).

Drives the new multi-select + bulk-defaults flow:

1. seed a work item with three runnable candidates,
2. open /tasks/<id>,
3. select two of the three rows,
4. paste a custom assertion into the bulk-defaults form,
5. click "Approve selected",
6. assert TEST-PLAN.json carries the new assertion on the two
   selected rows and the third stays untouched, and assert the
   outcomes summary listed both candidates as approved.

The third (unchecked) row guards against an off-by-one: a regression
that selects all rows would silently flip its decision too, so the
assertion is bidirectional.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from agentic_os.atomic_io import atomic_write_json
from agentic_os.events import EventLog
from agentic_os.paths import RuntimePaths
from agentic_os.plan_v2 import PlanItem, plan_to_json
from agentic_os.server import make_server
from agentic_os.storage.db import connect
from agentic_os.work_items import create_work_item_from_payload

from test_dashboard_server import _runtime, _free_port  # type: ignore[import-not-found]

pytestmark = pytest.mark.browser

sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright


CUSTOM_ASSERTION = "MUST return HTTP 200 OK with empty body when permission missing"


@pytest.fixture
def writable_dashboard(tmp_path: Path):
    paths = _runtime(tmp_path, enable_write=True)
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", paths
    finally:
        srv._shutdown_requested = True  # type: ignore[attr-defined]
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _seed_work_item(paths: RuntimePaths) -> str:
    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        detail = create_work_item_from_payload(
            conn,
            paths,
            events,
            {
                "title": "Bulk review regression",
                "priority": "P1",
                "business_goal": "Cover the multi-select bulk-defaults UI.",
                "expected_behavior": "Selecting two rows + applying a bulk assertion flips both decisions.",
                "relevant_surfaces": "GET /health, GET /users, GET /metrics",
            },
            default_sut_root=".",
        )
        return detail["work_item"]["id"]
    finally:
        conn.close()


def _seed_plan(paths: RuntimePaths, work_item_id: str) -> None:
    items = [
        PlanItem(
            candidate_id="c-health-get",
            title="GET /health returns 200",
            test_type="api",
            priority="P1",
            decision="needs_operator_decision",
            expected_assertion="GET /health must return HTTP 200",
            source_refs=["openapi:/health:get"],
            target_method="GET",
            target_path="/health",
            cleanup_strategy="read-only endpoint",
            functional_area="functional-system",
            lifecycle_tags=["regression"],
        ),
        PlanItem(
            candidate_id="c-users-get",
            title="GET /users returns a list",
            test_type="api",
            priority="P1",
            decision="needs_operator_decision",
            expected_assertion="GET /users must return HTTP 200 with a JSON array",
            source_refs=["openapi:/users:get"],
            target_method="GET",
            target_path="/users",
            cleanup_strategy="read-only endpoint",
            functional_area="functional-users",
            lifecycle_tags=["regression"],
        ),
        PlanItem(
            candidate_id="c-metrics-get",
            title="GET /metrics returns prometheus",
            test_type="api",
            priority="P1",
            decision="needs_operator_decision",
            expected_assertion="GET /metrics must return HTTP 200",
            source_refs=["openapi:/metrics:get"],
            target_method="GET",
            target_path="/metrics",
            cleanup_strategy="read-only endpoint",
            functional_area="functional-observability",
            lifecycle_tags=["regression"],
        ),
    ]
    plan_dir = paths.runtime_root / "plans" / work_item_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(plan_dir / "TEST-PLAN.json", plan_to_json(work_item_id, items))


def test_bulk_apply_assertion_to_selected_rows_only(writable_dashboard) -> None:
    base, paths = writable_dashboard
    work_item_id = _seed_work_item(paths)
    _seed_plan(paths, work_item_id)
    plan_path = paths.runtime_root / "plans" / work_item_id / "TEST-PLAN.json"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(f"{base}/tasks/{work_item_id}", wait_until="domcontentloaded")

            # Wait for the table to populate.
            page.locator("#task-candidates tbody tr").first.wait_for(timeout=5000)
            assert page.locator("#task-candidates tbody tr").count() == 3

            # Select two of the three rows by candidate-id.
            page.locator(
                "#task-candidates tbody tr[data-candidate-id='c-health-get'] input.candidate-checkbox"
            ).check()
            page.locator(
                "#task-candidates tbody tr[data-candidate-id='c-users-get'] input.candidate-checkbox"
            ).check()
            page.wait_for_function(
                "() => document.getElementById('bulk-count').textContent === '2 selected'",
                timeout=2000,
            )

            # Open the bulk-defaults panel and paste a custom assertion.
            page.locator("#bulk-defaults > summary").click()
            page.locator("#bulk-assertion").fill(CUSTOM_ASSERTION)

            approve_selected = page.locator("#approve-selected-candidates")
            page.wait_for_function(
                "() => !document.getElementById('approve-selected-candidates').disabled",
                timeout=2000,
            )
            approve_selected.click()

            # After the fan-out POSTs land and the table re-renders,
            # the two selected rows must be in generate_now AND carry
            # the bulk-overridden assertion. The third row must be
            # untouched.
            page.wait_for_function(
                "() => document.querySelectorAll("
                "'#task-candidates tbody .badge.badge-state-generate_now'"
                ").length === 2",
                timeout=5000,
            )

            final = json.loads(plan_path.read_text(encoding="utf-8"))
            by_id = {it["candidate_id"]: it for it in final["items"]}
            assert by_id["c-health-get"]["decision"] == "generate_now"
            assert by_id["c-users-get"]["decision"] == "generate_now"
            assert by_id["c-metrics-get"]["decision"] == "needs_operator_decision"
            assert by_id["c-health-get"]["expected_assertion"] == CUSTOM_ASSERTION
            assert by_id["c-users-get"]["expected_assertion"] == CUSTOM_ASSERTION
            assert (
                by_id["c-metrics-get"]["expected_assertion"]
                == "GET /metrics must return HTTP 200"
            ), "metrics row was not selected, its assertion must NOT have been touched"

            # The outcomes panel must render both approvals so the
            # operator gets a per-candidate audit trail (issue #138
            # "post-approve state").
            summary = page.locator("#candidate-summary")
            assert summary.get_attribute("hidden") is None, "summary panel should be visible"
            list_text = page.locator("#candidate-summary-list").text_content() or ""
            assert "c-health-get" in list_text
            assert "c-users-get" in list_text
            assert list_text.count("approved") >= 2

        finally:
            browser.close()


def test_inline_assertion_edit_persists_on_per_row_approve(writable_dashboard) -> None:
    """Editing the per-row textarea + clicking that row's Approve
    must persist the edited assertion (issue #138 inline editor)."""
    base, paths = writable_dashboard
    work_item_id = _seed_work_item(paths)
    _seed_plan(paths, work_item_id)
    plan_path = paths.runtime_root / "plans" / work_item_id / "TEST-PLAN.json"
    inline_assertion = "INLINE EDIT: return HTTP 200 with at least one row"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_context().new_page()
            page.goto(f"{base}/tasks/{work_item_id}", wait_until="domcontentloaded")
            row = page.locator(
                "#task-candidates tbody tr[data-candidate-id='c-users-get']"
            )
            row.locator("textarea.candidate-edit").wait_for(timeout=5000)
            row.locator("textarea.candidate-edit").fill(inline_assertion)
            # The first action button in the row is "Approve".
            row.locator("td:last-child button").first.click()
            page.wait_for_function(
                "() => Array.from(document.querySelectorAll("
                "  '#task-candidates tbody tr')).filter(r => "
                "  r.dataset.candidateId === 'c-users-get' && "
                "  r.querySelector('.badge-state-generate_now')"
                ").length === 1",
                timeout=5000,
            )
            final = json.loads(plan_path.read_text(encoding="utf-8"))
            by_id = {it["candidate_id"]: it for it in final["items"]}
            assert by_id["c-users-get"]["expected_assertion"] == inline_assertion
            assert by_id["c-users-get"]["decision"] == "generate_now"
        finally:
            browser.close()
