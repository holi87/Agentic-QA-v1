"""#367 — implementer + test-reviewer skills emit/audit Playwright + TypeScript.

The canonical generated stack is Playwright + TypeScript (ADR-0002,
`docs/standards/playwright-ts-standards.md`). The implementer
`implement-{api,ui}` skills (which tell the model HOW to write tests) and the
`reviewer-validate-tests` skill (which audits that test code) must speak the
live stack: a Java BDD reviewer hunting for `Thread.sleep` / `SoftAssertions`
on a `.spec.ts` suite misfires, and a Java implementer prompt emits code the
C2 lint gate + assertion-guard then reject. This gate pins the reframe so the
inherited Cucumber/RestAssured/Playwright-Java idioms cannot creep back.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"
PROVIDERS = ("claude", "codex", "gemini")

# Skills that produce or audit generated test code — must be PW+TS.
# The first three were reframed in #367; the rest of the planner/reviewer/
# implementer skills that touch generated test code were reframed in #409.
REFRAMED = (
    "implementer-implement-api",
    "implementer-implement-ui",
    "reviewer-validate-tests",
    # #409 — remaining test-code-bearing skills.
    "planner-design-features",
    "reviewer-validate-features",
    "reviewer-final-gate",
    "implementer-init-project",
    "implementer-verify",
    "implementer-package",
    "implementer-handle-flaky",
)

# Java BDD / Cucumber idioms that must NOT survive the reframe.
JAVA_IDIOMS = (
    "Thread.sleep",
    "RestAssured",
    "AssertJ",
    "SoftAssertions",
    "Picocontainer",
    "RequestSpecBuilder",
    "gradlew",
    "src/test/java",
    "src/test/resources",
    ".java",
    "Allure",
    "LogDetail",
)


@pytest.mark.parametrize("name", REFRAMED)
def test_reframed_skill_speaks_playwright_ts(name: str) -> None:
    for provider in PROVIDERS:
        text = (SKILLS_ROOT / provider / f"qc-{provider}-{name}.md").read_text(
            encoding="utf-8"
        )
        # Positive: anchored to the live Playwright + TypeScript stack.
        assert ".spec.ts" in text, f"{provider}/{name}: no .spec.ts reference"
        assert "playwright-ts-standards.md" in text, (
            f"{provider}/{name}: not anchored to docs/standards/playwright-ts-standards.md"
        )
        # Negative: no inherited Java BDD idioms.
        for idiom in JAVA_IDIOMS:
            assert idiom not in text, f"{provider}/{name}: Java idiom survives: {idiom!r}"


# #409 — the legacy Java BDD *toolchain* must not survive ANYWHERE in the skills
# corpus (planner/triager/reviewer included), not just the test-code skills.
# These are the structural idioms that are simply wrong on a Playwright+TS
# stack — every one has zero legitimate occurrences after the reframe.
#
# Deliberately NOT banned: the bare word "Cucumber" / the `cucumber-tags.md`
# filename. That standards file defines a tag taxonomy that carries over to
# Playwright `{ tag }` / `--grep`; the shipped gold implement-api/ui skills
# reference "Cucumber tag families (carried over to Playwright …)" and pass CI.
_JAVA_BDD_VOCAB = re.compile(
    r"\b(?:gherkin|maven|gradle|gradlew|junit|testng|picocontainer|"
    r"restassured|assertj|softassertions|allure|requestspecbuilder|logdetail)\b"
    r"|\.feature\b|\.java\b|src/test/(?:java|resources)|Thread\.sleep|libs\.versions\.toml",
    re.IGNORECASE,
)


def test_no_java_bdd_toolchain_survives_in_corpus() -> None:
    offenders = []
    for path in SKILLS_ROOT.rglob("qc-*.md"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _JAVA_BDD_VOCAB.search(line):
                offenders.append(f"{path.relative_to(SKILLS_ROOT)}:{i}: {line.strip()}")
    assert not offenders, "Java-BDD toolchain survives in the skills corpus (#409):\n" + "\n".join(
        offenders
    )
