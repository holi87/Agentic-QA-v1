"""Regression for issue #188.

The "Online URL SUT (no Docker)" YAML snippet in both operator-guide
twins must pass the strict config validator (i.e. include
`compose_project_name`, since the validator requires it even when
`mode: online`).
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import yaml

import pytest

from agentic_os.config import _validate

REPO_ROOT = Path(__file__).resolve().parents[1]
OPERATOR_GUIDES = (
    REPO_ROOT / "docs" / "operator-guide.md",
    REPO_ROOT / "docs" / "operator-guide_pl.md",
)


def _extract_yaml_blocks(md_text: str) -> list[str]:
    pattern = re.compile(r"```yaml\s*\n(.*?)```", re.DOTALL)
    return [m.group(1) for m in pattern.finditer(md_text)]


def _example_base_config() -> dict:
    example = REPO_ROOT / "config" / "agentic-os.yml.example"
    return yaml.safe_load(example.read_text(encoding="utf-8"))


@pytest.mark.parametrize("guide_path", OPERATOR_GUIDES, ids=lambda p: p.name)
def test_online_sut_snippet_in_operator_guide_validates(guide_path: Path) -> None:
    """Issue #188 — the documented online SUT YAML must pass `_validate`.

    We locate the YAML block that follows the "Online URL SUT" heading,
    merge it on top of the shipped example config, and assert the strict
    validator returns no errors.
    """
    if not guide_path.is_file():
        pytest.skip(f"{guide_path.name} not present")
    text = guide_path.read_text(encoding="utf-8")

    headings = ("## Online URL SUT", "## Online URL SUT (bez Dockera)")
    cut = -1
    for h in headings:
        idx = text.find(h)
        if idx != -1:
            cut = idx
            break
    assert cut != -1, f"online SUT heading not found in {guide_path.name}"

    after = text[cut:]
    blocks = _extract_yaml_blocks(after)
    assert blocks, f"no yaml block under online SUT section in {guide_path.name}"

    snippet = yaml.safe_load(blocks[0])
    assert isinstance(snippet, dict) and "sut" in snippet, (
        f"first yaml block under online SUT section in {guide_path.name} is "
        "expected to be a `sut:` snippet"
    )

    raw = _example_base_config()
    raw["sut"] = copy.deepcopy(snippet["sut"])

    errors = _validate(raw)
    assert errors == [], (
        f"documented online SUT snippet in {guide_path.name} fails strict "
        f"validation:\n\n" + "\n\n".join(errors)
    )
