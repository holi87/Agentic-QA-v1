"""STOP conditions + vague-verb discipline on the load-bearing skills.

The planner/reviewer/triager skills make autonomous decisions on ambiguous
input. Without explicit STOP conditions they guess; without concrete
thresholds ("≥ adequate", "versions sane") an LLM reviewer grades
decision-precision down. This gate asserts the dedicated `## STOP
conditions` section exists on those skills and that vague verbs are gone.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"
PROVIDERS = ("claude", "codex", "gemini")

# Skill names that must carry an explicit STOP-conditions contract.
STOP_REQUIRED = (
    "planner-analyze-task",
    "planner-design-features",
    "reviewer-final-gate",
    "reviewer-validate-features",
    "reviewer-validate-tests",
    "reviewer-validate-security",
    "triager-refine-bug",
    "triager-severity-priority",
)

_VAGUE_RE = re.compile(r"\b(adequate|sane|proper|appropriate)\b")
_STOP_SECTION_RE = re.compile(r"\n## STOP conditions\n(.*?)(?=\n## )", re.DOTALL)


def _stop_section(name: str, provider: str) -> str:
    text = (SKILLS_ROOT / provider / f"qc-{provider}-{name}.md").read_text(encoding="utf-8")
    match = _STOP_SECTION_RE.search(text)
    assert match, f"{provider}/{name}: no '## STOP conditions' section"
    return match.group(1)


@pytest.mark.parametrize("name", STOP_REQUIRED)
def test_stop_conditions_present_with_three_bullets(name: str) -> None:
    for provider in PROVIDERS:
        section = _stop_section(name, provider)
        bullets = [ln for ln in section.splitlines() if ln.lstrip().startswith("- ")]
        assert len(bullets) >= 3, (
            f"{provider}/{name}: STOP conditions needs ≥ 3 bullets, got {len(bullets)}"
        )


@pytest.mark.parametrize("name", STOP_REQUIRED)
def test_stop_conditions_identical_across_providers(name: str) -> None:
    sections = {p: _stop_section(name, p) for p in PROVIDERS}
    assert sections["codex"] == sections["claude"], name
    assert sections["gemini"] == sections["claude"], name


def test_no_vague_verbs_in_corpus() -> None:
    offenders = []
    for path in SKILLS_ROOT.rglob("qc-*.md"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _VAGUE_RE.search(line):
                offenders.append(f"{path.relative_to(SKILLS_ROOT)}:{i}")
    assert not offenders, "vague verbs found: " + ", ".join(offenders)
