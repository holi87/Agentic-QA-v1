"""Static review gate — diff scanning, AST/line heuristics.

Split from gates.py (issue #292).
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional

from .types import GateFinding, GateResult


def static_review_gate(diff_text: str, *, scope: str) -> GateResult:
    """Deterministic guardrail pass used before/without an external model."""
    if not diff_text.strip():
        return GateResult(
            verdict="REJECT",
            reason="empty_diff",
            findings=[GateFinding("diff", 1, "diff is empty")],
        )

    findings: List[GateFinding] = []
    current_file = "diff"
    hunk_file = "diff"
    old_line = 1
    new_line = 1
    hunk_known_bug_line: Optional[int] = None
    hunk_changed_line: Optional[int] = None
    hunk_has_decision = False

    def finish_hunk() -> None:
        if (
            scope == "assertion"
            and _is_test_path(hunk_file)
            and hunk_known_bug_line is not None
            and hunk_changed_line is not None
            and not hunk_has_decision
        ):
            findings.append(
                GateFinding(
                    hunk_file,
                    hunk_changed_line,
                    "modifying known-bug scenario requires explicit operator decision",
                )
            )

    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            finish_hunk()
            hunk_file = current_file
            hunk_known_bug_line = None
            hunk_changed_line = None
            hunk_has_decision = False
            continue
        if raw.startswith("+++ b/"):
            finish_hunk()
            current_file = raw[6:]
            hunk_file = current_file
            hunk_known_bug_line = None
            hunk_changed_line = None
            hunk_has_decision = False
            new_line = 1
            continue
        if raw.startswith("@@"):
            finish_hunk()
            hunk_file = current_file
            hunk_known_bug_line = None
            hunk_changed_line = None
            hunk_has_decision = False
            old_line, new_line = _parse_hunk_lines(raw)
            continue
        if _has_operator_decision_marker(raw):
            hunk_has_decision = True
        if raw.startswith(" ") and _has_known_bug_pair(raw):
            hunk_known_bug_line = new_line
            old_line += 1
            new_line += 1
            continue
        if raw.startswith("-") and not raw.startswith("---") and _has_known_bug_pair(raw):
            hunk_known_bug_line = old_line
        if raw.startswith("+") and not raw.startswith("+++"):
            added = raw[1:]
            _scan_added_line(findings, current_file, new_line, added)
            if hunk_changed_line is None:
                hunk_changed_line = new_line
            new_line += 1
            continue
        if raw.startswith("-") and not raw.startswith("---"):
            removed = raw[1:]
            if hunk_changed_line is None:
                hunk_changed_line = max(new_line, 1)
            _scan_removed_line(findings, current_file, max(old_line, 1), removed)
            old_line += 1
            continue
        if not raw.startswith("\\"):
            old_line += 1
            new_line += 1
    finish_hunk()

    if scope not in {"api", "ui", "assertion", "final", "general"}:
        findings.append(GateFinding("gate", 1, f"unknown review scope: {scope}"))

    if findings:
        return GateResult(verdict="REJECT", reason=_reason_for(findings), findings=findings)
    return GateResult(verdict="APPROVE", reason="static_checks_passed", findings=[])


def _parse_hunk_lines(hunk: str) -> tuple[int, int]:
    match = re.search(r"@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@", hunk)
    if not match:
        return 1, 1
    return int(match.group(1)), int(match.group(2))


def _scan_added_line(findings: List[GateFinding], path: str, line: int, text: str) -> None:
    stripped = text.lstrip()
    if stripped.startswith(("'", '"')):
        return
    if _KNOWN_BUG_RE.search(text) and _BUG_TAG_RE.search(text) is None:
        findings.append(GateFinding(path, line, "@known-bug requires paired @bug-NNN tag"))
    for pattern, message in _SKIP_PATTERNS:
        if pattern.search(text) and not _has_operator_decision_marker(text):
            findings.append(GateFinding(path, line, message))
    checks: Iterable[tuple[str, str]] = (
        ("shell=True", "subprocess must not use shell=True"),
        ("os.system(", "os.system is forbidden; use argv subprocess wrapper"),
        ("subprocess.call(", "direct subprocess calls must go through runtime.subprocess"),
        ("subprocess.run(", "direct subprocess calls must go through runtime.subprocess"),
        ("require_reports_on_failure: false", "reports cannot be disabled on failure"),
        ("known_bugs_fail_exit: false", "known bugs must remain red"),
        ("assert True", "trivial assertions are forbidden"),
        ("assertTrue(true", "trivial assertions are forbidden"),
    )
    for needle, message in checks:
        if needle in text:
            findings.append(GateFinding(path, line, message))
    if _is_generator_path(path) and _RAW_GENERATOR_INTERPOLATION_RE.search(text):
        findings.append(
            GateFinding(
                path,
                line,
                "generator JS interpolation must use js_str(), not quoted f-string placeholders",
            )
        )
    if _UNTRUSTED_PROMPT_SOURCE_RE.search(text) and "wrap_untrusted" not in text:
        findings.append(
            GateFinding(
                path,
                line,
                "untrusted SUT/test-output text must be wrapped with wrap_untrusted() before prompt use",
            )
        )


def _scan_removed_line(findings: List[GateFinding], path: str, line: int, text: str) -> None:
    if _is_assertion_line(text):
        findings.append(GateFinding(path, line, "removed assertion requires explicit decision"))


def _reason_for(findings: List[GateFinding]) -> str:
    joined = " ".join(f.message for f in findings)
    if "assertion" in joined:
        return "assertion_weakened"
    if "skip" in joined or "xit" in joined or "xdescribe" in joined:
        return "skip_without_decision"
    if "@known-bug" in joined or "known-bug" in joined:
        return "known_bug_requires_decision"
    if "subprocess" in joined or "os.system" in joined:
        return "unsafe_subprocess"
    if "reports" in joined or "known bugs" in joined:
        return "report_gate_weakened"
    if "generator JS interpolation" in joined:
        return "generator_interpolation"
    if "wrap_untrusted" in joined:
        return "untrusted_prompt_input"
    return "static_gate_failed"


def _has_operator_decision_marker(text: str) -> bool:
    return _OPERATOR_DECISION_RE.search(text) is not None


def _has_known_bug_pair(text: str) -> bool:
    return _KNOWN_BUG_RE.search(text) is not None and _BUG_TAG_RE.search(text) is not None


def _is_test_path(path: str) -> bool:
    lowered = path.lower()
    return (
        "/test" in lowered
        or lowered.startswith("test")
        or lowered.endswith((".feature", ".spec.ts", ".spec.js", ".test.ts", ".test.js", "_test.py"))
    )


def _is_generator_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return "/generators/" in normalized or normalized.startswith(
        "scripts/agentic-os/agentic_os/generators/"
    )


def _is_assertion_line(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return any(pattern.search(stripped) for pattern in _ASSERTION_PATTERNS)


_BUG_TAG_RE = re.compile(r"@bug-\d{3,}\b", re.IGNORECASE)


_KNOWN_BUG_RE = re.compile(r"@known-bug\b", re.IGNORECASE)


_OPERATOR_DECISION_RE = re.compile(
    r"<!--\s*agentic-os:\s*decision\s+"
    r"id=DEC-[A-Za-z0-9_.:-]+\s+"
    r"actor=operator\s+"
    r"hmac=[a-fA-F0-9]{64}\s*-->"
)


_RAW_GENERATOR_INTERPOLATION_RE = re.compile(
    r"""f(?:'[^'\n]*?|\"[^\"\n]*?)['"`]\{[^}\n]+\}['"`]"""
)


_UNTRUSTED_PROMPT_SOURCE_RE = re.compile(
    r"""failure\[['"]message['"]\]|failure\.get\(['"](?:error_message|message|stack_head)['"]\)|page\.title\(\)|response\.text\(\)"""
)


_SKIP_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bxit\s*\("), "xit requires explicit operator decision"),
    (re.compile(r"\bxdescribe\s*\("), "xdescribe requires explicit operator decision"),
    (re.compile(r"\b(?:test|it|describe)\.skip\s*\("), "test skip requires explicit operator decision"),
    (re.compile(r"\bpytest\.mark\.(?:skip|skipif|xfail)\b"), "pytest skip/xfail requires explicit operator decision"),
)


_ASSERTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*assert\b"),
    re.compile(r"\bself\.assert[A-Z]\w*\s*\("),
    re.compile(r"\bassert(?:That|Equals|Equal|True|False|Throws|All|NotNull|Null)?\s*\("),
    re.compile(r"\bAssertions\.(?:assert\w+|assertThat)\s*\("),
    re.compile(r"\bassertions\.[A-Za-z_]\w*\s*\("),
    re.compile(r"\bassert\.(?:equal|deepEqual|strictEqual|isTrue|isFalse|throws|include|match)\s*\("),
    re.compile(r"\b(?:chai\.)?expect(?:\.soft)?\s*(?:\(|\{)"),
    re.compile(r"\bshould(?:\s+|\.)"),
    re.compile(r"\.should(?:\s+|\.)"),
    re.compile(r"\bis_expected\.(?:to|not_to)\b"),
    re.compile(r"^\s*\.(?:to|not_to|resolves|rejects)\b"),
    re.compile(r"^\s*\.(?:toBe|toEqual|toContain|toHave|toThrow|isEqualTo|contains|hasSize)\b"),
    re.compile(r"\bwith\s+pytest\.raises\b"),
    re.compile(r"\bpytest\.raises\s*\("),
)
