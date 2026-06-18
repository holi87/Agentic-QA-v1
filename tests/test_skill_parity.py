"""Cross-provider skill parity.

Every skill name ships as a claude/codex/gemini triplet. The three files
share one body; only three regions may differ:

- the frontmatter ``name:`` value (provider prefix),
- the ``# Skill:`` H1 (provider prefix),
- the ``## Communication`` preamble (provider-specific runtime block).

Beyond those, a small number of *legitimate* provider-capability
divergences exist — e.g. ``AskUserQuestion`` is a Claude-only tool, so the
codex/gemini ``implementer-init-project`` variants STOP with
``needs_input`` instead. We therefore assert a line-count threshold rather
than byte-identity: large drift (a dropped commit step, a missing
STATUS.md row) is caught, while a couple of capability-driven lines are
tolerated.

This catches the silent drift an LLM reviewer flags on first read.
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path

import pytest

PROVIDERS = ("claude", "codex", "gemini")
SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"

# Max normalized body lines a provider variant may differ from claude.
# Current legitimate max is 2 (the AskUserQuestion divergence in
# implementer-init-project); the buffer absorbs future capability-driven
# differences. Real drift (final-gate/implementer-verify pre-fix) was 20+
# lines, so this stays comfortably below it.
MAX_BODY_DRIFT_LINES = 6

_COMMUNICATION_RE = re.compile(r"\n## Communication\n.*?(?=\n## )", re.DOTALL)


def _skill_names() -> list[str]:
    """Skill name buckets present for every provider."""
    per_provider = {}
    for provider in PROVIDERS:
        prefix = f"qc-{provider}-"
        per_provider[provider] = {
            path.name[len(prefix) : -3]
            for path in (SKILLS_ROOT / provider).glob(f"{prefix}*.md")
        }
    return sorted(set.intersection(*per_provider.values()))


def _normalize(text: str, provider: str) -> list[str]:
    """Strip provider-specific regions, leaving only the shared body."""
    without_comm = _COMMUNICATION_RE.sub("\n## Communication\n<PREAMBLE>\n", text)
    return without_comm.replace(f"qc-{provider}-", "qc-PROVIDER-").splitlines()


def _changed_lines(baseline: list[str], other: list[str]) -> list[str]:
    return [
        line
        for line in difflib.unified_diff(baseline, other, lineterm="", n=0)
        if line and line[0] in "+-" and not line.startswith(("+++", "---"))
    ]


SKILL_NAMES = _skill_names()


def test_every_skill_has_full_provider_triplet() -> None:
    for provider in PROVIDERS:
        for name in SKILL_NAMES:
            path = SKILLS_ROOT / provider / f"qc-{provider}-{name}.md"
            assert path.is_file(), f"missing provider variant: {path}"


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skill_body_parity_within_threshold(name: str) -> None:
    baseline = _normalize(
        (SKILLS_ROOT / "claude" / f"qc-claude-{name}.md").read_text(encoding="utf-8"),
        "claude",
    )
    for provider in ("codex", "gemini"):
        variant = _normalize(
            (SKILLS_ROOT / provider / f"qc-{provider}-{name}.md").read_text(encoding="utf-8"),
            provider,
        )
        changed = _changed_lines(baseline, variant)
        assert len(changed) <= MAX_BODY_DRIFT_LINES, (
            f"{name}: {provider} body drifted {len(changed)} lines from claude "
            f"(max {MAX_BODY_DRIFT_LINES}), outside the Communication preamble:\n"
            + "\n".join(changed)
        )
