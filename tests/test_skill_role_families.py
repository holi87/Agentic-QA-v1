"""New role+verb skill families (#264) and their integration wiring.

The audit found five recurring autonomous decisions with no skill prompt:
handle-flaky, validate-architecture, link-duplicates, escalate-blocker, and
recover-from-quota. This gate asserts each ships as a full provider triplet
with the STOP + Example contract, is enabled in config, and that the two
integration points (final-gate -> validate-architecture, first-check ->
link-duplicates) are wired.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"
PROVIDERS = ("claude", "codex", "gemini")

NEW_FAMILIES = (
    "implementer-handle-flaky",
    "reviewer-validate-architecture",
    "triager-link-duplicates",
    "triager-escalate-blocker",
    "recover-from-quota",
)


@pytest.mark.parametrize("name", NEW_FAMILIES)
def test_new_family_ships_full_triplet_with_contract(name: str) -> None:
    for provider in PROVIDERS:
        path = SKILLS_ROOT / provider / f"qc-{provider}-{name}.md"
        assert path.is_file(), f"missing {path}"
        text = path.read_text(encoding="utf-8")
        assert "${include_preamble}" in text, f"{name}/{provider}: no preamble directive"
        assert "## STOP conditions" in text, f"{name}/{provider}: no STOP conditions"
        assert "## Example" in text, f"{name}/{provider}: no Example"
        assert "## When to use" in text, f"{name}/{provider}: no When to use"


def test_new_families_enabled_in_config() -> None:
    cfg = yaml.safe_load((REPO_ROOT / "config" / "skills.yml").read_text(encoding="utf-8"))
    enabled = {
        sid
        for role in cfg["skills"]["per_role"].values()
        for sid in role.get("enabled", [])
    }
    for name in NEW_FAMILIES:
        for provider in PROVIDERS:
            sid = f"{provider}/qc-{provider}-{name}"
            assert sid in enabled, f"{sid} not enabled in config/skills.yml"


def test_final_gate_delegates_architecture_slice() -> None:
    for provider in PROVIDERS:
        text = (SKILLS_ROOT / provider / f"qc-{provider}-reviewer-final-gate.md").read_text(
            encoding="utf-8"
        )
        assert "reviewer-validate-architecture" in text, provider


def test_first_check_calls_link_duplicates() -> None:
    for provider in PROVIDERS:
        text = (SKILLS_ROOT / provider / f"qc-{provider}-triager-first-check.md").read_text(
            encoding="utf-8"
        )
        assert "triager-link-duplicates" in text, provider
