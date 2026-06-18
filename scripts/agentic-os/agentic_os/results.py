"""result parser + bug adjudication.

Parses JUnit XML, Playwright JSON, and Cucumber JSON reports into a uniform
in-memory representation and classifies each failure into one of:

- `product_bug`      — exact-spec failure (assertion stated in plan rang true,
  but the SUT misbehaved). Opens a Markdown bug + DB row downstream.
- `known_bug_red`    — known bug still red; exit code stays 1 (no greenwash).
- `infra`            — runner crash / docker missing / network refused.
- `flaky`            — alternates pass/fail across reruns; no bug but flagged.
- `test_bug`         — assertion error consistent with a wrong test
  expectation; routes back to plan owner.

The parsers are pure: input bytes, output dataclasses. Adjudication is also
pure — it takes a list of results plus context (known bug IDs etc.) and
returns classification + a proposed bug payload when applicable.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional


@dataclass(frozen=True)
class TestResult:
    __test__ = False  # pytest must not collect this dataclass as a test class
    name: str
    suite: str
    status: str  # passed | failed | skipped | error
    duration_ms: int = 0
    failure_message: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    runner: str = "unknown"


@dataclass(frozen=True)
class Classification:
    name: str
    suite: str
    category: str  # product_bug | known_bug_red | infra | flaky | test_bug | pass | skip
    reason: str
    bug_id: Optional[str] = None


@dataclass(frozen=True)
class BugReport:
    bug_id: str
    title: str
    severity: str
    body: str  # full Markdown


def parse_junit_xml(data: bytes) -> List[TestResult]:
    """Parse JUnit XML. Tolerant of `testsuite` or `testsuites` root."""
    root = ET.fromstring(data)
    suites = [root] if root.tag == "testsuite" else list(root.findall(".//testsuite"))
    results: List[TestResult] = []
    for suite in suites:
        suite_name = suite.get("name") or "junit"
        for case in suite.findall("testcase"):
            name = case.get("name") or "(unnamed)"
            classname = case.get("classname") or suite_name
            duration_s = float(case.get("time") or 0.0)
            failure_el = case.find("failure")
            error_el = case.find("error")
            skipped_el = case.find("skipped")
            status = "passed"
            message: Optional[str] = None
            if failure_el is not None:
                status = "failed"
                message = (failure_el.get("message") or failure_el.text or "").strip() or None
            elif error_el is not None:
                status = "error"
                message = (error_el.get("message") or error_el.text or "").strip() or None
            elif skipped_el is not None:
                status = "skipped"
            tags = _extract_tags(name)
            results.append(
                TestResult(
                    name=name,
                    suite=classname,
                    status=status,
                    duration_ms=int(duration_s * 1000),
                    failure_message=message,
                    tags=tags,
                    runner="junit",
                )
            )
    return results


def parse_playwright_json(data: bytes) -> List[TestResult]:
    payload = json.loads(data.decode("utf-8"))
    results: List[TestResult] = []
    for suite in payload.get("suites") or []:
        _walk_playwright_suite(suite, parent="", out=results)
    return results


def _walk_playwright_suite(suite: dict, *, parent: str, out: List[TestResult]) -> None:
    title = suite.get("title") or "playwright"
    here = f"{parent} > {title}".strip(" >") if parent else title
    for spec in suite.get("specs") or []:
        spec_title = spec.get("title") or "(spec)"
        for test in spec.get("tests") or []:
            for result in test.get("results") or []:
                status_raw = (result.get("status") or "passed").lower()
                status = {
                    "passed": "passed",
                    "failed": "failed",
                    "timedOut": "failed",
                    "interrupted": "error",
                    "skipped": "skipped",
                }.get(status_raw, status_raw)
                message: Optional[str] = None
                errors = result.get("errors") or []
                if errors:
                    message = (errors[0].get("message") or "").strip() or None
                out.append(
                    TestResult(
                        name=spec_title,
                        suite=here,
                        status=status,
                        duration_ms=int((result.get("duration") or 0)),
                        failure_message=message,
                        tags=_extract_tags(spec_title),
                        runner="playwright",
                    )
                )
    for child in suite.get("suites") or []:
        _walk_playwright_suite(child, parent=here, out=out)


def parse_cucumber_json(data: bytes) -> List[TestResult]:
    payload = json.loads(data.decode("utf-8"))
    results: List[TestResult] = []
    for feature in payload:
        feature_name = feature.get("name") or feature.get("uri") or "cucumber"
        for element in feature.get("elements") or []:
            name = element.get("name") or "(scenario)"
            tags = [t.get("name") for t in (element.get("tags") or []) if t.get("name")]
            steps = element.get("steps") or []
            status = "passed"
            message: Optional[str] = None
            duration = 0
            for step in steps:
                step_status = (step.get("result") or {}).get("status") or "passed"
                duration += int((step.get("result") or {}).get("duration") or 0)
                if step_status in {"failed", "undefined", "ambiguous"}:
                    status = "failed"
                    message = (step.get("result") or {}).get("error_message") or None
                    break
                if step_status == "skipped" and status == "passed":
                    status = "skipped"
            results.append(
                TestResult(
                    name=name,
                    suite=feature_name,
                    status=status,
                    duration_ms=duration // 1_000_000,  # cucumber returns nanos
                    failure_message=message,
                    tags=tags,
                    runner="cucumber",
                )
            )
    return results


def _extract_tags(name: str) -> List[str]:
    return re.findall(r"@[\w-]+", name)


def classify_results(
    results: Iterable[TestResult],
    *,
    known_bug_ids: Optional[Iterable[str]] = None,
    flaky_names: Optional[Iterable[str]] = None,
) -> List[Classification]:
    known = set(known_bug_ids or [])
    flaky = set(flaky_names or [])
    out: List[Classification] = []
    for r in results:
        if r.status == "passed":
            out.append(Classification(r.name, r.suite, "pass", "passed"))
            continue
        if r.status == "skipped":
            out.append(Classification(r.name, r.suite, "skip", "skipped"))
            continue
        if r.name in flaky:
            out.append(Classification(r.name, r.suite, "flaky", "matches flaky allowlist"))
            continue
        msg = (r.failure_message or "").lower()
        if any(hint in msg for hint in (
            "connection refused",
            "econnrefused",
            "no such host",
            "docker",
            "network unreachable",
            "playwright is not installed",
        )):
            out.append(Classification(r.name, r.suite, "infra", "infra hint in failure message"))
            continue
        bug_hits = re.findall(r"@bug-\d+", " ".join(r.tags))
        known_hit = next((b for b in bug_hits if b in known), None)
        if known_hit:
            out.append(
                Classification(r.name, r.suite, "known_bug_red", "known bug still red", bug_id=known_hit)
            )
            continue
        if r.status == "error":
            out.append(Classification(r.name, r.suite, "test_bug", "runner error — likely test bug"))
            continue
        # Default for a clean assertion failure: product bug (exact-spec).
        out.append(
            Classification(
                r.name,
                r.suite,
                "product_bug",
                "assertion failure matches plan expectation",
            )
        )
    return out


def render_bug_markdown(
    *,
    bug_id: str,
    title: str,
    severity: str,
    test_result: TestResult,
    expected: str,
    actual: str,
    evidence_paths: Iterable[str],
    repro_steps: Iterable[str],
) -> BugReport:
    evidence = list(evidence_paths) or ["(no evidence captured)"]
    repro = list(repro_steps) or ["(see test source)"]
    lines = [
        f"# {bug_id} — {title}",
        "",
        f"- Severity: `{severity}`",
        f"- Suite: `{test_result.suite}`",
        f"- Test name: `{test_result.name}`",
        f"- Runner: `{test_result.runner}`",
        "",
        "## Expected",
        "",
        expected.strip(),
        "",
        "## Actual",
        "",
        actual.strip(),
        "",
        "## Evidence",
        "",
        *(f"- `{p}`" for p in evidence),
        "",
        "## Repro",
        "",
        *(f"{i + 1}. {step}" for i, step in enumerate(repro)),
        "",
    ]
    body = "\n".join(lines)
    return BugReport(bug_id=bug_id, title=title, severity=severity, body=body)


def next_bug_id(existing_ids: Iterable[str]) -> str:
    """Return `BUG-NNN` one above the max numeric id in existing."""
    nums = []
    for bid in existing_ids:
        m = re.match(r"BUG-(\d+)", bid)
        if m:
            nums.append(int(m.group(1)))
    n = max(nums) + 1 if nums else 1
    return f"BUG-{n:03d}"


def summarize_classifications(items: Iterable[Classification]) -> Dict[str, int]:
    counts: Dict[str, int] = {
        "pass": 0, "skip": 0, "product_bug": 0, "known_bug_red": 0,
        "infra": 0, "flaky": 0, "test_bug": 0,
    }
    for c in items:
        counts[c.category] = counts.get(c.category, 0) + 1
    counts["total"] = sum(counts.values()) - counts.get("total", 0)
    return counts
