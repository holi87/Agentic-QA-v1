"""Quantitative API and UI coverage-depth gate verdict mapping."""
from __future__ import annotations

from agentic_os.coverage_review import (
    API_COMPANION_MARKERS,
    UI_FLOOR_MARKERS,
    evaluate_api_coverage,
    evaluate_ui_coverage,
)
from agentic_os.generators.api import generate_api_test
from agentic_os.generators.ui import generate_ui_test
from agentic_os.plan_v2 import PlanItem


def _ui_item(**overrides) -> PlanItem:
    base = dict(
        candidate_id="UI-233",
        title="Order list",
        test_type="ui",
        priority="P1",
        decision="generate_now",
        expected_assertion='URL must contain /orders and text "Orders" must be visible',
        source_refs=["docs/requirements.md#L1"],
        target_page="/orders",
        notes=[],
    )
    base.update(overrides)
    return PlanItem(**base)


def _api_item(**overrides) -> PlanItem:
    base = dict(
        candidate_id="API-233",
        title="List orders",
        test_type="api",
        priority="P1",
        decision="generate_now",
        expected_assertion="HTTP 200 and body.orders present",
        source_refs=["docs/api.md#L1"],
        target_method="GET",
        target_path="/orders",
        notes=[],
    )
    base.update(overrides)
    return PlanItem(**base)


# --- UI verdict mapping ----------------------------------------------------

def test_ui_pass_when_all_floor_markers_present() -> None:
    spec = generate_ui_test(_ui_item(), coverage_floor=True).content
    verdict = evaluate_ui_coverage(spec, coverage_floor=True, target_page="/orders")
    assert verdict.verdict == "PASS"
    assert verdict.reason is None
    assert verdict.missing == ()


def test_ui_reject_when_floor_missing_and_flag_on() -> None:
    spec = generate_ui_test(_ui_item()).content  # flag off → no markers
    verdict = evaluate_ui_coverage(spec, coverage_floor=True, target_page="/orders")
    assert verdict.verdict == "REJECT"
    assert verdict.reason == "coverage_floor_missing"
    assert "agentic-os:floor:console" in verdict.missing


def test_ui_pass_warn_when_floor_missing_and_flag_off() -> None:
    spec = generate_ui_test(_ui_item()).content
    verdict = evaluate_ui_coverage(spec, coverage_floor=False, target_page="/orders")
    assert verdict.verdict == "PASS_WARN"
    assert verdict.reason == "coverage_floor_missing"


def test_ui_reject_when_business_assertion_missing() -> None:
    spec = "// stripped spec\nawait page.goto('/orders');\n"
    verdict = evaluate_ui_coverage(spec, coverage_floor=True, target_page="/orders")
    assert verdict.verdict == "REJECT"
    assert verdict.reason == "business_assertion_missing"


def test_ui_form_target_does_not_require_link_walk() -> None:
    spec = generate_ui_test(
        _ui_item(
            target_page="/orders/new",
            notes=["no-link-walk"],
        ),
        coverage_floor=True,
    ).content
    verdict = evaluate_ui_coverage(spec, coverage_floor=True, target_page="/orders/new")
    assert verdict.verdict == "PASS"


# --- API verdict mapping ---------------------------------------------------

def test_api_get_with_credentials_pass_when_companions_present() -> None:
    spec = generate_api_test(
        _api_item(), credentials_env="SESSION", coverage_floor=True
    ).content
    verdict = evaluate_api_coverage(
        spec, coverage_floor=True, method="GET", credentials_set=True
    )
    assert verdict.verdict == "PASS"


def test_api_get_with_credentials_reject_when_neg_auth_missing() -> None:
    spec = generate_api_test(_api_item(), coverage_floor=False).content  # baseline
    verdict = evaluate_api_coverage(
        spec, coverage_floor=True, method="GET", credentials_set=True
    )
    assert verdict.verdict == "REJECT"
    assert verdict.reason == "coverage_floor_missing"
    assert "agentic-os:companion:neg-auth" in verdict.missing


def test_api_post_requires_boundary_companion() -> None:
    post_item = PlanItem(
        candidate_id="API-POST",
        title="Create order",
        test_type="api",
        priority="P1",
        decision="generate_now",
        expected_assertion="HTTP 201 and body.id present",
        source_refs=["docs/api.md#L80"],
        target_method="POST",
        target_path="/orders",
        required_test_data='{"qty": 1}',
        cleanup_strategy="DELETE /orders/{id}",
    )
    spec = generate_api_test(post_item, coverage_floor=True).content
    verdict = evaluate_api_coverage(
        spec, coverage_floor=True, method="POST", credentials_set=False
    )
    assert verdict.verdict == "PASS"


def test_api_reject_when_status_assertion_missing() -> None:
    spec = "// stripped\nconst r = await ctx.get('/orders');\n"
    verdict = evaluate_api_coverage(spec, coverage_floor=True, method="GET")
    assert verdict.verdict == "REJECT"
    assert verdict.reason == "business_assertion_missing"


def test_api_pass_warn_when_flag_off_and_companions_missing() -> None:
    spec = generate_api_test(_api_item()).content
    verdict = evaluate_api_coverage(
        spec, coverage_floor=False, method="GET", credentials_set=True
    )
    assert verdict.verdict == "PASS_WARN"
    assert verdict.reason == "coverage_floor_missing"


# --- Marker contract guards ------------------------------------------------

def test_ui_floor_marker_constants_are_stable() -> None:
    """Reviewer skill greps these literal strings — must not drift."""
    assert UI_FLOOR_MARKERS == (
        "agentic-os:floor:console",
        "agentic-os:floor:network",
        "agentic-os:floor:a11y",
        "agentic-os:floor:link-walk",
    )


def test_api_companion_marker_constants_are_stable() -> None:
    assert API_COMPANION_MARKERS == (
        "agentic-os:companion:neg-auth",
        "agentic-os:companion:bola",
        "agentic-os:companion:boundary",
        "agentic-os:companion:injection",
        "agentic-os:companion:schema",
    )


# ---------------------------------------------------------------------------
# Issue #287 — coverage_gap producer wired into run_review_gate (record-only).
# ---------------------------------------------------------------------------

import sqlite3  # noqa: E402
from pathlib import Path  # noqa: E402


def _review_runtime(tmp_path: Path):
    from agentic_os.events import EventLog
    from agentic_os.orchestrator import Orchestrator
    from agentic_os.paths import RuntimePaths
    from agentic_os.storage import init_db

    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    return conn, paths, events, orch


def _write_diff(paths, rel_in_repo: str, body: str) -> Path:
    target = paths.repo_root / rel_in_repo
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return Path(rel_in_repo)


# A minimal unified diff that adds an API spec with a status assertion but no
# coverage-floor companion markers. The static gate approves it (assertion
# present, no skip patterns); the C2 producer then sees coverage_floor_missing.
_API_DIFF_MISSING_COMPANIONS = """\
diff --git a/tests/api/orders.spec.ts b/tests/api/orders.spec.ts
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/tests/api/orders.spec.ts
@@ -0,0 +1,5 @@
+import { test, expect } from '@playwright/test';
+test('GET /orders returns 200', async ({ request }) => {
+  const response = await request.get('/orders');
+  expect( response.status() ).toBe(200);
+});
"""


def _coverage_gap_rows(conn, scope: str):
    return conn.execute(
        "SELECT subject, payload FROM learnings WHERE kind='coverage_gap' AND subject LIKE ?;",
        (f"%{scope}%",),
    ).fetchall()


def test_review_gate_records_coverage_gap_when_floor_missing(tmp_path: Path) -> None:
    from agentic_os.workflows import run_review_gate

    conn, paths, events, orch = _review_runtime(tmp_path)
    try:
        diff_rel = _write_diff(paths, "tests/api/orders.spec.ts.diff", _API_DIFF_MISSING_COMPANIONS)
        result = run_review_gate(
            orch,
            paths,
            events,
            diff_path=diff_rel,
            scope="api",
        )
        # Record-only: the gate verdict is unchanged (the static gate approves
        # this benign assertion-bearing diff); the producer must not flip it.
        assert result.ok is True
        rows = _coverage_gap_rows(conn, "api")
        assert len(rows) == 1
        import json as _json

        payload = _json.loads(rows[0]["payload"])
        assert payload["scope"] == "api"
        assert payload["missing"]
    finally:
        conn.close()


def test_review_gate_no_coverage_gap_for_non_ui_api_scope(tmp_path: Path) -> None:
    from agentic_os.workflows import run_review_gate

    conn, paths, events, orch = _review_runtime(tmp_path)
    try:
        diff_rel = _write_diff(paths, "tests/api/orders.spec.ts.diff", _API_DIFF_MISSING_COMPANIONS)
        run_review_gate(
            orch,
            paths,
            events,
            diff_path=diff_rel,
            scope="assertion",
        )
        assert _coverage_gap_rows(conn, "assertion") == []
    finally:
        conn.close()
