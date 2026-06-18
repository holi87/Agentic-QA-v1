"""Every skill ships a concrete `## Example`.

An LLM reviewer auto-grades the corpus 1/5 on examples when no skill
demonstrates the artifact it asks the agent to produce. This gate asserts
each skill has an `## Example` section with at least one fenced code block,
that the example is identical across the three provider variants, and that
the machine-readable examples (JSON / YAML) actually parse.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"
PROVIDERS = ("claude", "codex", "gemini")

_EXAMPLE_RE = re.compile(r"\n## Example\n(.*)\Z", re.DOTALL)
_FENCE_RE = re.compile(r"```(\w+)?\n(.*?)\n```", re.DOTALL)


def _skill_names() -> list[str]:
    prefix = "qc-claude-"
    return sorted(
        p.name[len(prefix) : -3]
        for p in (SKILLS_ROOT / "claude").glob(f"{prefix}*.md")
    )


def _example_section(name: str, provider: str) -> str:
    text = (SKILLS_ROOT / provider / f"qc-{provider}-{name}.md").read_text(encoding="utf-8")
    match = _EXAMPLE_RE.search(text)
    assert match, f"{provider}/{name}: no '## Example' section"
    return match.group(1)


SKILL_NAMES = _skill_names()


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skill_has_example_with_fenced_block(name: str) -> None:
    for provider in PROVIDERS:
        section = _example_section(name, provider)
        assert _FENCE_RE.search(section), f"{provider}/{name}: no fenced code block in ## Example"


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_example_identical_across_providers(name: str) -> None:
    sections = {p: _example_section(name, p) for p in PROVIDERS}
    assert sections["codex"] == sections["claude"], name
    assert sections["gemini"] == sections["claude"], name


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_example_fenced_payloads_parse(name: str) -> None:
    section = _example_section(name, "claude")
    for lang, payload in _FENCE_RE.findall(section):
        if lang == "json":
            json.loads(payload)
        elif lang == "yaml":
            loaded = yaml.safe_load(payload)
            assert loaded is not None, f"{name}: empty YAML example"
