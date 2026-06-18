"""TEST-PLAN.json schema validation, review gate, and summary behavior."""
from __future__ import annotations

import json

from agentic_os.plan_v2 import (
    PlanItem,
    plan_to_json,
    review_gate_verdict,
    summarize_plan,
    validate_plan,
)


def _ok_api_item(**overrides) -> PlanItem:
    base = dict(
        candidate_id="C-001",
        title="Reject negative quantity",
        test_type="api",
        priority="P1",
        decision="generate_now",
        expected_assertion="POST /orders with quantity=-1 must return HTTP 400 and error.code=invalid_quantity",
        source_refs=["docs/openapi.yaml#/paths/~1orders/post", "docs/requirements.md#L42"],
        target_method="POST",
        target_path="/orders",
        required_test_data="quantity=-1",
        cleanup_strategy="DELETE /orders/{id} on success path; nothing to clean on rejection",
        negative_or_boundary=True,
        generator_target="playwright-ts",
        # Issue #105 — functional/lifecycle metadata is required for
        # `generate_now` items.
        functional_area="functional-orders",
        lifecycle_tags=["regression"],
    )
    base.update(overrides)
    return PlanItem(**base)


def test_well_formed_api_item_passes() -> None:
    findings = validate_plan([_ok_api_item()])
    assert findings == []
    verdict, reason = review_gate_verdict(findings)
    assert verdict == "APPROVE"
    assert reason == "plan_review_passed"


def test_missing_source_ref_blocks() -> None:
    findings = validate_plan([_ok_api_item(source_refs=[])])
    assert any("source_ref" in f.message for f in findings)
    verdict, _ = review_gate_verdict(findings)
    assert verdict == "REJECT"


def test_trivial_assertion_blocks() -> None:
    findings = validate_plan(
        [_ok_api_item(expected_assertion="response.ok")]
    )
    assert any("trivial" in f.message for f in findings)
    verdict, _ = review_gate_verdict(findings)
    assert verdict == "REJECT"


def test_mutating_requires_cleanup() -> None:
    findings = validate_plan([_ok_api_item(cleanup_strategy=" ")])
    messages = " | ".join(f.message for f in findings)
    assert "cleanup_strategy" in messages
    verdict, _ = review_gate_verdict(findings)
    assert verdict == "REJECT"


def test_ui_without_page_warns() -> None:
    findings = validate_plan(
        [
            _ok_api_item(
                candidate_id="C-UI-1",
                test_type="ui",
                target_method=None,
                target_path=None,
                cleanup_strategy=None,
            )
        ]
    )
    messages = " | ".join(f.message for f in findings)
    assert "target_page" in messages


def test_decisions_other_than_generate_now_skip_strict_checks() -> None:
    item = _ok_api_item(
        decision="blocked_missing_docs",
        source_refs=[],
        expected_assertion="",
    )
    findings = validate_plan([item])
    assert findings == []


def test_summary_counters() -> None:
    summary = summarize_plan(
        [
            _ok_api_item(candidate_id="A", decision="generate_now"),
            _ok_api_item(candidate_id="B", decision="needs_operator_decision"),
            _ok_api_item(candidate_id="C", decision="blocked_missing_docs"),
        ]
    )
    assert summary["total"] == 3
    assert summary["generate_now"] == 1
    assert summary["needs_operator_decision"] == 1
    assert summary["blocked_missing_docs"] == 1


def test_plan_json_is_serializable() -> None:
    payload = plan_to_json("TASK-001", [_ok_api_item()])
    encoded = json.dumps(payload)
    assert "TASK-001" in encoded
    assert "playwright-ts" in encoded


def test_invalid_test_type_blocks() -> None:
    findings = validate_plan([_ok_api_item(test_type="weird")])
    assert any("test_type" in f.message for f in findings)


def test_invalid_generator_target_blocks() -> None:
    findings = validate_plan([_ok_api_item(generator_target="cypress")])
    assert any("generator_target" in f.message for f in findings)
