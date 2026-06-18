"""Issue #293 — architecture context doc + token-bounded prompt injection."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentic_os.architecture_context import (
    DEFAULT_BUDGET_TOKENS,
    architecture_context_block,
    extract_summary,
)
from agentic_os.budgets import estimate_tokens
from agentic_os.security import redact_sensitive_text

REPO_ROOT = Path(__file__).resolve().parents[1]
_MARKERS = (
    "<!-- inject:architecture-summary:start -->",
    "<!-- inject:architecture-summary:end -->",
)


@pytest.mark.parametrize("doc", ["architecture.md", "architecture_pl.md"])
def test_doc_twin_has_inject_markers_and_block(doc: str) -> None:
    text = (REPO_ROOT / "docs" / doc).read_text(encoding="utf-8")
    for marker in _MARKERS:
        assert marker in text, f"{doc} missing {marker}"
    summary = extract_summary(text)
    assert summary, f"{doc} has an empty inject block"


def test_extract_summary_returns_none_without_markers() -> None:
    assert extract_summary("# doc with no markers\n\nbody") is None


def test_injected_block_is_within_default_budget() -> None:
    block = architecture_context_block(REPO_ROOT)
    assert block is not None
    assert estimate_tokens(block) <= DEFAULT_BUDGET_TOKENS


def test_compressed_summary_is_far_smaller_than_full_doc() -> None:
    """Acceptance: measured token delta (compressed vs raw)."""
    doc_text = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    summary = extract_summary(doc_text)
    assert summary is not None
    raw_tokens = estimate_tokens(doc_text)
    summary_tokens = estimate_tokens(summary)
    # The compressed form must be a large reduction (documented ~83%).
    assert summary_tokens < raw_tokens // 3


def test_injected_block_is_redaction_safe() -> None:
    """The block flows through redact_prompt; it must carry no secret-shaped
    literals that would be mangled to [REDACTED]."""
    block = architecture_context_block(REPO_ROOT)
    assert block is not None
    assert redact_sensitive_text(block) == block


def test_block_mentions_core_runtime_facts() -> None:
    block = architecture_context_block(REPO_ROOT) or ""
    for token in ("work_item", "_invoke_attempt", "analyze", "review"):
        assert token in block, token


def test_missing_doc_returns_none(tmp_path: Path) -> None:
    # No docs/architecture.md under this root → skip injection, no crash.
    assert architecture_context_block(tmp_path) is None


def test_prompt_context_config_validates_and_rejects_bad_types(tmp_path: Path) -> None:
    """Issue #293 — prompt_context is an accepted optional config section, but
    its values are type-checked."""
    from agentic_os.config import load_config
    from agentic_os.errors import ConfigError

    base = (REPO_ROOT / "config" / "agentic-os.yml.example").read_text(encoding="utf-8")
    good = tmp_path / "good.yml"
    good.write_text(base, encoding="utf-8")
    cfg = load_config(good)
    assert cfg.raw["prompt_context"]["architecture_enabled"] is True

    bad = tmp_path / "bad.yml"
    bad.write_text(
        base.replace(
            "architecture_budget_tokens: 600",
            'architecture_budget_tokens: "lots"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(bad)


def test_budget_bounds_oversized_summary(tmp_path: Path) -> None:
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir(parents=True)
    big = "word " * 4000
    (doc_dir / "architecture.md").write_text(
        f"{_MARKERS[0]}\n{big}\n{_MARKERS[1]}\n", encoding="utf-8"
    )
    block = architecture_context_block(tmp_path, budget_tokens=50)
    assert block is not None
    assert estimate_tokens(block) <= 50 + 30  # block budget + wrapper headroom
    assert block.rstrip().endswith("…")
