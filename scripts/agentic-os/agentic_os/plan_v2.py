"""test planning v2.

Adds a structured `TEST-PLAN.json` next to the existing markdown plan, with a
review gate that blocks weak items before the API/UI generators run.

A plan item is one candidate test that the API/UI generator could produce.
Fields capture everything Codex needs to verify generated code against the
plan: source reference, expected assertion, test data strategy, decision.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple


PLAN_VERSION = "1.0"


_VALID_TEST_TYPES = {"api", "ui", "security", "accessibility"}
_VALID_DECISIONS = {
    "generate_now",
    "needs_operator_decision",
    "blocked_missing_docs",
    "not_testable",
}
_VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}
_VALID_GENERATOR_TARGETS = {"playwright-ts", "pytest-httpx"}
_MUTATING_METHODS = {"post", "put", "patch", "delete"}


@dataclass(frozen=True)
class PlanItem:
    candidate_id: str
    title: str
    test_type: str
    priority: str
    decision: str
    expected_assertion: str
    source_refs: List[str]
    target_method: Optional[str] = None  # for API tests
    target_path: Optional[str] = None    # for API tests
    target_page: Optional[str] = None    # for UI tests
    required_test_data: Optional[str] = None
    cleanup_strategy: Optional[str] = None  # required for mutating endpoints
    known_bug_relation: Optional[str] = None
    negative_or_boundary: bool = False
    generator_target: str = "playwright-ts"
    # Issue #105 — every generated test must carry exactly one
    # `@functional-<area>` tag and at least one lifecycle tag so the
    # QualityCat invariants apply to AI-generated tests too.
    functional_area: Optional[str] = None
    lifecycle_tags: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class PlanFinding:
    candidate_id: str
    severity: str  # P0 blocker | P1 issue | P2 nit
    message: str


def validate_plan(items: Iterable[PlanItem]) -> List[PlanFinding]:
    """Run plan review gate. Empty list = approved."""
    findings: List[PlanFinding] = []
    for item in items:
        if item.test_type not in _VALID_TEST_TYPES:
            findings.append(
                PlanFinding(item.candidate_id, "P0", f"invalid test_type: {item.test_type!r}")
            )
        if item.priority not in _VALID_PRIORITIES:
            findings.append(
                PlanFinding(item.candidate_id, "P0", f"invalid priority: {item.priority!r}")
            )
        if item.decision not in _VALID_DECISIONS:
            findings.append(
                PlanFinding(item.candidate_id, "P0", f"invalid decision: {item.decision!r}")
            )
        if item.generator_target not in _VALID_GENERATOR_TARGETS:
            findings.append(
                PlanFinding(item.candidate_id, "P0", f"invalid generator_target: {item.generator_target!r}")
            )
        # Issue #107 — `pytest-httpx` is config-allowed for forward
        # compatibility but the generator only emits Playwright TS.
        # Block `generate_now` until a pytest-httpx generator exists.
        if (
            item.decision == "generate_now"
            and item.generator_target == "pytest-httpx"
        ):
            findings.append(
                PlanFinding(
                    item.candidate_id,
                    "P0",
                    "generator_target=pytest-httpx is not implemented; "
                    "switch to playwright-ts or remove the candidate",
                )
            )
        # Issue #106 — security/accessibility candidates have no
        # implementation path yet. Block `generate_now` so operators
        # cannot finalize a phase that depends on absent generators.
        if (
            item.decision == "generate_now"
            and item.test_type in {"security", "accessibility"}
        ):
            findings.append(
                PlanFinding(
                    item.candidate_id,
                    "P0",
                    f"test_type={item.test_type} has no generator; "
                    "block generate_now until a generator is added or "
                    "convert to a manual review candidate",
                )
            )
        if item.decision != "generate_now":
            # Items not in generate_now do not need full content yet.
            continue
        # Issue #105 — functional/lifecycle metadata is required for
        # generated tests too. The plan must carry it so generators
        # can emit Playwright annotations and triage can enforce tags.
        functional = (item.functional_area or "").strip().lstrip("@")
        if not functional or not functional.startswith("functional-"):
            # Tolerate either `functional-orders` or the operator
            # writing the bare area name `orders` — but reject empty.
            if not functional:
                findings.append(
                    PlanFinding(
                        item.candidate_id,
                        "P0",
                        "generate_now requires a functional_area tag "
                        "(e.g. functional-orders)",
                    )
                )
        if not item.lifecycle_tags:
            findings.append(
                PlanFinding(
                    item.candidate_id,
                    "P0",
                    "generate_now requires at least one lifecycle_tag "
                    "(e.g. smoke, regression)",
                )
            )
        if not item.source_refs:
            findings.append(
                PlanFinding(item.candidate_id, "P0", "generate_now requires at least one source_ref")
            )
        if not item.expected_assertion.strip():
            findings.append(
                PlanFinding(item.candidate_id, "P0", "generate_now requires non-empty expected_assertion")
            )
        elif _is_trivial_assertion(item.expected_assertion):
            findings.append(
                PlanFinding(
                    item.candidate_id,
                    "P0",
                    "expected_assertion is trivial (status 2xx alone is not enough)",
                )
            )
        if item.test_type == "api":
            method = (item.target_method or "").lower()
            if not _has_http_status_assertion(item.expected_assertion):
                findings.append(
                    PlanFinding(
                        item.candidate_id,
                        "P0",
                        "api generate_now requires an explicit HTTP status assertion",
                    )
                )
            if method in _MUTATING_METHODS and not (item.cleanup_strategy or "").strip():
                findings.append(
                    PlanFinding(
                        item.candidate_id,
                        "P0",
                        f"mutating {method.upper()} requires cleanup_strategy",
                    )
                )
        if item.test_type == "ui":
            if not item.target_page:
                findings.append(
                    PlanFinding(item.candidate_id, "P1", "ui test requires target_page")
                )
            if not _has_ui_assertion_target(item.expected_assertion):
                findings.append(
                    PlanFinding(
                        item.candidate_id,
                        "P0",
                        "ui generate_now requires URL, text, or role/name assertion target",
                    )
                )
    return findings


def _has_http_status_assertion(text: str) -> bool:
    import re

    return bool(
        re.search(r"\bHTTP\s*\d{3}\b", text, re.IGNORECASE)
        or re.search(r"\bstatus(?:_code|\s+code)?\s*[:=]?\s*\d{3}\b", text, re.IGNORECASE)
    )


def _has_ui_assertion_target(text: str) -> bool:
    import re

    return bool(
        re.search(r"\bURL\s+(?:must\s+)?(?:contain|equal|match)\s+\S+", text, re.IGNORECASE)
        or re.search(r"\btext\s+[\"'][^\"']+[\"']", text, re.IGNORECASE)
        or re.search(
            r"\brole\s+[\"'][^\"']+[\"']\s+name\s+[\"'][^\"']+[\"']",
            text,
            re.IGNORECASE,
        )
    )


def _is_trivial_assertion(text: str) -> bool:
    """Detect assertion text the generator cannot turn into a meaningful check.

    Wave 13 (#313 / RC gap 6) — fallback assertions like "not 5xx", "URL is
    not error/404", or bare "2xx response" are too weak for an RC test
    suite: they pass for any non-error response, which is not a
    meaningful assertion for a release candidate. Detecting them here
    makes ``validate_plan`` emit a P0 finding, which
    ``patch_builder._try_generate_v2`` already converts into a
    ``needs_operator_decision`` outcome — the operator must rewrite the
    assertion before generation proceeds, matching the issue's
    "require operator approval, not silently shipped" invariant.
    """
    import re as _re

    normalized = " ".join(text.strip().lower().split())
    trivial_patterns = (
        "response.ok",
        "status 2xx",
        "status < 500",
        "no error",
        "assert true",
        "expect(true)",
        # Wave 13 / RC gap 6 — fallback "not X" patterns the generator
        # used to accept. They pass for any non-error response, which is
        # too weak for release-candidate gating.
        "not 5xx",
        "not 500",
        "not 404",
        "not error",
        "not server error",
        "is not error",
        # Bare "2xx response" without an explicit status code is the same
        # weak fallback baked into exploratory.py's safe bucket pre-Wave 13.
        "2xx response",
    )
    if any(p in normalized for p in trivial_patterns) and len(normalized) < 60:
        return True
    # "URL is not /404/" / "URL must not contain /500/" — semantically the
    # same fallback, but the URL phrasing escapes the substring match
    # above. Catch the negation form explicitly.
    if _re.search(
        r"\burl\b.*\b(?:is\s+not|must\s+not\s+contain|must\s+not\s+equal)\b",
        normalized,
    ):
        return True
    return False


def plan_to_json(task_id: str, items: Iterable[PlanItem]) -> Dict[str, Any]:
    """Serialize the plan to TEST-PLAN.json shape."""
    serialized = []
    for item in items:
        d = asdict(item)
        d["source_refs"] = list(d["source_refs"])
        d["notes"] = list(d["notes"])
        serialized.append(d)
    return {
        "version": PLAN_VERSION,
        "task_id": task_id,
        "items": serialized,
    }


def summarize_plan(items: Iterable[PlanItem]) -> Dict[str, int]:
    """Counters for dashboard summary."""
    items = list(items)
    counts = {decision: 0 for decision in _VALID_DECISIONS}
    for it in items:
        if it.decision in counts:
            counts[it.decision] += 1
    counts["total"] = len(items)
    return counts


def review_gate_verdict(findings: List[PlanFinding]) -> Tuple[str, str]:
    """Return (verdict, reason) for the plan review gate."""
    if not findings:
        return ("APPROVE", "plan_review_passed")
    blockers = [f for f in findings if f.severity == "P0"]
    if blockers:
        return ("REJECT", f"plan_review_blockers:{len(blockers)}")
    return ("APPROVE", f"plan_review_passed_with_warnings:{len(findings)}")
