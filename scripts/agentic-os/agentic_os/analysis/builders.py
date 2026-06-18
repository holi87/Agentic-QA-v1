"""Requirements / risk-map / candidate-test builders.

Split from analysis.py (issue #292).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from ..time_utils import now_iso

from .extractors import _default_cleanup_for_method, _derive_api_expected_assertion, _extract_api_mentions, _extract_ui_routes, _has_ui_intent, _is_negative_or_boundary, _priority_for_text, _spec_sections, _surface_enabled
from .types import _AnalysisInputs


def _build_requirements(inputs: _AnalysisInputs) -> str:
    sections = _spec_sections(inputs.spec_markdown)
    lines: List[str] = [
        f"# Requirements — {inputs.work_item['title']}",
        "",
        "_Derived from task spec; not authoritative — operator must reconcile._",
        "",
        "## Business goal",
        sections.get("business goal", "_not provided in spec_"),
        "",
        "## Expected behavior",
        sections.get("expected behavior", "_not provided in spec_"),
        "",
        "## In scope",
        sections.get("in scope", "_not provided in spec_"),
        "",
        "## Out of scope",
        sections.get("out of scope", "_not provided in spec_"),
        "",
        "## Known bugs",
        sections.get("known bugs", "_not provided in spec_"),
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _build_risk_map(inputs: _AnalysisInputs, sut_map: Dict[str, Any]) -> str:
    text = inputs.spec_markdown
    risks: List[str] = []
    if _SECURITY_HINT.search(text):
        risks.append("- Security: spec mentions auth/credentials. Treat negative paths as P1.")
    if "known bugs" in inputs.spec_markdown.lower():
        risks.append("- Known bugs declared by operator — confirm before drafting tests.")
    if not sut_map.get("config_snapshot", {}).get("openapi"):
        risks.append("- No OpenAPI source: API coverage is heuristic until operator confirms.")
    if not any(s.get("label") == "tests_dir" and s.get("status") == "dir" for s in sut_map["sources"]):
        risks.append("- Existing tests directory not located: regression risk is unmeasured.")
    if not risks:
        risks.append("- No structural risks detected from spec alone.")
    lines = [
        f"# Risk map — {inputs.work_item['title']}",
        "",
        "Heuristic checklist. Each bullet is a candidate for operator review.",
        "",
    ] + risks + [""]
    return "\n".join(lines).rstrip() + "\n"


def _build_candidate_tests(
    inputs: _AnalysisInputs,
    sut_map: Dict[str, Any],
) -> tuple[str, Dict[str, Any], Dict[str, int]]:
    spec = inputs.spec_markdown
    buckets: Dict[str, List[str]] = {b: [] for b in CANDIDATE_BUCKETS}
    structured: List[Dict[str, Any]] = []
    api_enabled = _surface_enabled(sut_map, "api", default=True)
    web_enabled = _surface_enabled(sut_map, "web", default=True)

    raw_endpoints = _extract_api_mentions(spec)
    endpoints = raw_endpoints if api_enabled else []
    if raw_endpoints and not api_enabled:
        buckets["Needs operator decision"].append(
            "Task text contains API-like routes, but `sut.api.enabled=false`; "
            "API candidates were not generated."
        )
    for idx, endpoint in enumerate(endpoints, start=1):
        label = f"{endpoint['method']} {endpoint['path']}"
        expected = _derive_api_expected_assertion(spec, endpoint["method"], endpoint["path"])
        buckets["API"].append(f"`{label}` — {expected}")
        structured.append(
            {
                "candidate_id": f"API-SPEC-{idx:03d}",
                "bucket": "API",
                "test_type": "api",
                "title": f"{label} contract from task spec",
                "priority": _priority_for_text(spec, default="P2"),
                "decision": "needs_operator_decision",
                "expected_assertion": expected,
                "source_refs": [f"{inputs.work_item['spec_path']}#api-mention-{idx}"],
                "target_method": endpoint["method"],
                "target_path": endpoint["path"],
                "required_test_data": endpoint.get("required_test_data"),
                "cleanup_strategy": _default_cleanup_for_method(endpoint["method"], expected),
                "negative_or_boundary": _is_negative_or_boundary(expected),
                "generator_target": "playwright-ts",
                "notes": ["Derived from task spec. Operator must approve before generation."],
            }
        )

    ui_intent = _has_ui_intent(spec)
    ui_routes = _extract_ui_routes(spec) if ui_intent else []
    if ui_intent and not ui_routes:
        ui_routes = [None]
    if ui_routes and not web_enabled:
        buckets["Needs operator decision"].append(
            "Task text contains UI routes, but `sut.web.enabled=false`; "
            "UI candidates were not generated."
        )
    elif ui_routes:
        for idx, target_page in enumerate(ui_routes[:10], start=1):
            expected = (
                f"URL must contain {target_page}"
                if target_page and target_page != "/"
                else "Operator must define visible text, role/name, or URL assertion"
            )
            label = target_page or "flow"
            buckets["UI"].append(
                f"`{label}` — surface-level interaction coverage for UI elements named in spec."
            )
            structured.append(
                {
                    "candidate_id": f"UI-SPEC-{idx:03d}",
                    "bucket": "UI",
                    "test_type": "ui",
                    "title": f"UI flow from task spec ({label})",
                    "priority": _priority_for_text(spec, default="P2"),
                    "decision": "needs_operator_decision",
                    "expected_assertion": expected,
                    "source_refs": [f"{inputs.work_item['spec_path']}#ui-mention-{idx}"],
                    "target_page": target_page,
                    "required_test_data": "Operator must define UI fixture data.",
                    "cleanup_strategy": "read-only UI navigation"
                    if target_page
                    else "Operator must define cleanup for created records.",
                    "negative_or_boundary": False,
                    "generator_target": "playwright-ts",
                    "notes": ["Derived from UI keywords/routes in task spec."],
                }
            )
    if _A11Y_HINT.search(spec):
        buckets["Accessibility"].append("Confirm semantic structure and assistive-tech support.")
        structured.append(
            {
                "candidate_id": "A11Y-SPEC-001",
                "bucket": "Accessibility",
                "test_type": "accessibility",
                "title": "Accessibility risk from task spec",
                "priority": "P2",
                "decision": "needs_operator_decision",
                "expected_assertion": "Operator must define WCAG/ARIA expectation before generation",
                "source_refs": [f"{inputs.work_item['spec_path']}#accessibility-mention"],
                "generator_target": "playwright-ts",
                "notes": ["Not generated automatically in this release path."],
            }
        )
    if _SECURITY_HINT.search(spec):
        buckets["Security"].append("Negative auth, token-handling, and injection probes.")
        structured.append(
            {
                "candidate_id": "SEC-SPEC-001",
                "bucket": "Security",
                "test_type": "security",
                "title": "Security risk from task spec",
                "priority": "P1",
                "decision": "needs_operator_decision",
                "expected_assertion": "Operator must define exact security expectation before generation",
                "source_refs": [f"{inputs.work_item['spec_path']}#security-mention"],
                "generator_target": "playwright-ts",
                "notes": ["Security probes require explicit operator approval."],
            }
        )

    has_configured_test_surface = api_enabled and bool(sut_map.get("openapi_inventory"))
    if not endpoints and not ui_routes and not has_configured_test_surface:
        buckets["Not testable now"].append(
            "Spec lacks an enumerable interface to exercise — operator must clarify."
        )
        structured.append(
            {
                "candidate_id": "NTN-SPEC-001",
                "bucket": "Not testable now",
                "test_type": "api",
                "title": "Spec lacks enumerable interface",
                "priority": "P3",
                "decision": "not_testable",
                "expected_assertion": "Operator must provide API endpoint, UI route, or docs source.",
                "source_refs": [inputs.work_item["spec_path"]],
                "generator_target": "playwright-ts",
                "notes": ["Blocked until operator clarifies testable surface."],
            }
        )
    if "TBD" in spec or "?" in spec:
        buckets["Needs operator decision"].append(
            "Spec contains `TBD` or open questions — operator confirmation required."
        )
    if api_enabled and not any(
        s.get("label") == "openapi" and s.get("status") == "file" for s in sut_map["sources"]
    ):
        buckets["Needs operator decision"].append(
            "OpenAPI source not configured — operator must confirm API contracts."
        )

    summary = {b: len(items) for b, items in buckets.items()}
    summary["Structured candidates"] = len(structured)
    candidates_payload = {
        "version": "1.0",
        "work_item_id": inputs.work_item["id"],
        "generated_at": now_iso(),
        "items": structured,
        "summary": summary,
    }
    lines = [
        f"# Candidate tests — {inputs.work_item['title']}",
        "",
        "Buckets follow the analysis contract (API / UI / Accessibility / Security /",
        "Not testable now / Needs operator decision).",
        "",
    ]
    for bucket in CANDIDATE_BUCKETS:
        lines.append(f"## {bucket}")
        items = buckets[bucket]
        if not items:
            lines.append("- (none derived)")
        else:
            lines.extend(items)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n", candidates_payload, summary


CANDIDATE_BUCKETS = (
    "API",
    "UI",
    "Accessibility",
    "Security",
    "Not testable now",
    "Needs operator decision",
)


_A11Y_HINT = re.compile(r"\b(a11y|accessibility|screen[- ]reader|wcag|aria)\b", re.IGNORECASE)


_SECURITY_HINT = re.compile(r"\b(auth|token|csrf|xss|sql injection|injection|secret|password|credential)\b", re.IGNORECASE)
