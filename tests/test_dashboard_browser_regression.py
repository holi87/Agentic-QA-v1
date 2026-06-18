"""Browser regression for the dashboard candidate review flow (issue #135).

Spins up the real dashboard server against a tmp runtime, seeds a work item
with two runnable candidates, drives headless chromium through the operator
golden path (open task detail → click "Approve all runnable" → table reloads
with badges flipped to ``generate_now``).

Opt-in via the ``browser`` pytest marker so default CI does not require the
Playwright browser bundle. Run locally with::

    .venv/bin/pip install pytest-playwright
    .venv/bin/playwright install chromium
    .venv/bin/pytest -m browser tests/test_dashboard_browser_regression.py
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

# Skip the whole module when the Playwright Python package or its browser
# bundle is missing. This keeps the default ``pytest`` invocation green even
# on hosts that never opted in via ``playwright install chromium``.
sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright


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
                "title": "Dashboard browser regression",
                "priority": "P1",
                "business_goal": "Cover review-table golden flow end-to-end.",
                "expected_behavior": "POST /api/tasks/<id>/candidates/approve-all flips both rows to generate_now.",
                "relevant_surfaces": "GET /health, GET /users",
            },
            default_sut_root=".",
        )
        return detail["work_item"]["id"]
    finally:
        conn.close()


def _seed_test_plan(paths: RuntimePaths, work_item_id: str) -> None:
    """Write a valid TEST-PLAN.json with two runnable candidates.

    Both candidates start as ``needs_operator_decision`` so the
    ``approve-all`` endpoint will flip them to ``generate_now`` and exercise
    the post-approve re-render path the issue is regression-protecting.
    """
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
    ]
    plan_dir = paths.runtime_root / "plans" / work_item_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(plan_dir / "TEST-PLAN.json", plan_to_json(work_item_id, items))


def test_dashboard_approve_all_flips_both_badges(writable_dashboard) -> None:
    base, paths = writable_dashboard
    work_item_id = _seed_work_item(paths)
    _seed_test_plan(paths, work_item_id)

    # Sanity: the API surface the JS bundle calls actually serves the seed.
    plan_path = paths.runtime_root / "plans" / work_item_id / "TEST-PLAN.json"
    seeded = json.loads(plan_path.read_text(encoding="utf-8"))
    assert len(seeded["items"]) == 2
    assert all(it["decision"] == "needs_operator_decision" for it in seeded["items"])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(f"{base}/tasks/{work_item_id}", wait_until="domcontentloaded")

            # Two real candidate rows render after the JS bundle fetches the
            # plan. Use locator auto-waiting — no time.sleep.
            badges = page.locator("#task-candidates tbody tr .badge")
            badges.first.wait_for(state="visible", timeout=5000)
            assert badges.count() == 2
            assert (
                page.locator(
                    "#task-candidates tbody .badge.badge-state-needs_operator_decision"
                ).count()
                == 2
            )

            approve_all = page.locator("#approve-all-candidates")
            approve_all.wait_for(state="visible")
            # Wait until the JS bundle has resolved /api/config and decided
            # the button is enabled. ``writable=true`` means it will flip
            # from disabled→enabled on first render.
            page.wait_for_function(
                "() => !document.getElementById('approve-all-candidates').disabled",
                timeout=5000,
            )
            approve_all.click()

            # After approve-all returns, the JS re-renders the table.
            # The badges flipping to ``generate_now`` is the deterministic
            # post-condition; the ``#candidate-msg`` status line briefly
            # carries ``msg ok`` but ``renderTaskCandidates`` resets it to
            # ``msg muted`` on the same tick, so racing it is unreliable.
            page.wait_for_function(
                "() => document.querySelectorAll("
                "'#task-candidates tbody .badge.badge-state-generate_now'"
                ").length === 2",
                timeout=5000,
            )

            # And the source of truth on disk reflects the approve.
            final = json.loads(plan_path.read_text(encoding="utf-8"))
            decisions = sorted(it["decision"] for it in final["items"])
            assert decisions == ["generate_now", "generate_now"]
        finally:
            browser.close()
