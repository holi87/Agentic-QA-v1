"""Regression for issue #189.

Every `docs/...md` path referenced in the operator-guide twins must
resolve to a file that exists on disk — the documented operator flow
needs to be copy/paste runnable.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OPERATOR_GUIDES = (
    REPO_ROOT / "docs" / "operator-guide.md",
    REPO_ROOT / "docs" / "operator-guide_pl.md",
)


@pytest.mark.parametrize("guide_path", OPERATOR_GUIDES, ids=lambda p: p.name)
def test_operator_guide_doc_references_resolve(guide_path: Path) -> None:
    """Issue #189 — `docs/...md` references in the operator guide must exist.

    We deliberately scan the whole document because every such reference
    in a shell snippet is intended to be runnable as-is by the operator.
    """
    if not guide_path.is_file():
        pytest.skip(f"{guide_path.name} not present")
    text = guide_path.read_text(encoding="utf-8")

    refs = re.findall(r"docs/[\w\-/.]+\.md", text)
    assert refs, f"no docs/...md references found in {guide_path.name}"

    missing: list[str] = []
    for ref in sorted(set(refs)):
        target = REPO_ROOT / ref
        if not target.is_file():
            missing.append(ref)

    assert not missing, (
        f"{guide_path.name} references non-existent docs file(s): {missing}"
    )
