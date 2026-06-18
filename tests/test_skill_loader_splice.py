"""Skill loader splice directives + shared-standards references.

The on-disk skill body stays deduplicated: the per-provider
``## Communication`` preamble is a single ``${include_preamble}`` directive
resolved against ``skills/_preamble_<provider>.md`` at load time. This keeps
the corpus DRY while the model still sees the fully-inlined prompt.

These tests pin the splice mechanics (and their path-traversal guard) and
assert the corpus stays consistent: no unresolved directives leak into a
composed prompt, and every skill that uses the BIZ/TECH descriptor anchors
to the canonical standard.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentic_os.skills import (
    discover_skills,
    list_skills,
    load_skill,
    resolve_skill_includes,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"
PROVIDERS = ("claude", "codex", "gemini")

# Skill names whose Communication block is shared cross-provider (byte
# identical across claude/codex/gemini) rather than provider-specific.
# They intentionally stay inline — there is no per-provider preamble to
# splice. test_inline_preamble_skills_share_identical_communication guards
# the assumption that justifies the exemption.
_INLINE_PREAMBLE = {"planner-coverage-architect"}

_BIZ_TECH_USAGE_RE = re.compile(r"\.as\(['\"]?(BIZ|TECH)")
_COMMUNICATION_RE = re.compile(r"## Communication\n(.*?)(?=\n## )", re.DOTALL)


def _preamble_text(provider: str) -> str:
    return (SKILLS_ROOT / f"_preamble_{provider}.md").read_text(encoding="utf-8").strip()


def _migrated_skill_ids() -> list[str]:
    ids = []
    for provider in PROVIDERS:
        for path in sorted((SKILLS_ROOT / provider).glob(f"qc-{provider}-*.md")):
            name = path.name[len(f"qc-{provider}-") : -3]
            if name not in _INLINE_PREAMBLE:
                ids.append(f"{provider}/{path.name[:-3]}".replace(".md", ""))
    return ids


def _all_skill_ids() -> list[str]:
    return [s.skill_id for s in discover_skills(SKILLS_ROOT, include_root=REPO_ROOT)]


# --- splice mechanics ------------------------------------------------------


def test_include_preamble_resolves_to_provider_fragment() -> None:
    out = resolve_skill_includes(
        "## Communication\n\n${include_preamble}\n\n## Next",
        "claude/qc-claude-anything",
        SKILLS_ROOT,
        REPO_ROOT,
    )
    assert out == f"## Communication\n\n{_preamble_text('claude')}\n\n## Next"


def test_include_path_resolves_file_contents() -> None:
    out = resolve_skill_includes(
        "${include: docs/standards/biz-tech-assertions.md}",
        "claude/x",
        SKILLS_ROOT,
        REPO_ROOT,
    )
    assert out.startswith("# BIZ/TECH Assertion Descriptors")


def test_include_path_traversal_is_blocked() -> None:
    with pytest.raises(ValueError, match="escapes include root"):
        resolve_skill_includes(
            "${include: ../../../etc/passwd}", "claude/x", SKILLS_ROOT, REPO_ROOT
        )


def test_included_secret_is_rejected_after_expansion(tmp_path) -> None:
    skills = tmp_path / "skills" / "claude"
    skills.mkdir(parents=True)
    (tmp_path / "secret.md").write_text("api_key=abcdef123456\n", encoding="utf-8")
    (skills.parent / "claude" / "leak.md").write_text(
        "---\nname: leak\n---\n\n# Skill: leak\n\n${include: secret.md}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="secret"):
        load_skill(tmp_path / "skills", "claude/leak.md", include_root=tmp_path)


def test_global_scope_resolves_includes_from_global_dir(tmp_path) -> None:
    global_dir = tmp_path / "global"
    (global_dir / "claude").mkdir(parents=True)
    (global_dir / "fragments").mkdir()
    (global_dir / "fragments" / "note.md").write_text("SHARED NOTE\n", encoding="utf-8")
    (global_dir / "claude" / "y.md").write_text(
        "---\nname: y\n---\n\n# Skill: y\n\n${include: fragments/note.md}\n",
        encoding="utf-8",
    )
    config = {
        "skills": {
            "scope": "global",
            "project_dir": "skills",
            "global_dir": str(global_dir),
            "per_role": {"planner": {"enabled": ["claude/y"], "disabled": []}},
        }
    }
    resolution = list_skills("planner", config=config, project_root=tmp_path)
    assert resolution.warnings == []
    assert resolution.skills and "SHARED NOTE" in resolution.skills[0].body


def test_include_missing_file_raises() -> None:
    with pytest.raises(ValueError, match="not found"):
        resolve_skill_includes(
            "${include: docs/standards/does-not-exist.md}",
            "claude/x",
            SKILLS_ROOT,
            REPO_ROOT,
        )


def test_unknown_provider_preamble_raises() -> None:
    with pytest.raises(ValueError, match="not found"):
        resolve_skill_includes(
            "${include_preamble}", "mystery/x", SKILLS_ROOT, REPO_ROOT
        )


# --- corpus invariants -----------------------------------------------------


@pytest.mark.parametrize("skill_id", _migrated_skill_ids())
def test_migrated_skill_inlines_canonical_preamble(skill_id: str) -> None:
    provider = skill_id.split("/", 1)[0]
    rel = skill_id.split("/", 1)[1] + ".md"
    body = load_skill(SKILLS_ROOT, f"{provider}/{rel}", include_root=REPO_ROOT).body
    assert _preamble_text(provider) in body
    assert "${include" not in body


def test_no_unresolved_directives_in_corpus() -> None:
    for skill in discover_skills(SKILLS_ROOT, include_root=REPO_ROOT):
        assert "${include" not in skill.body, skill.skill_id


def test_discover_skips_preamble_fragments() -> None:
    ids = _all_skill_ids()
    assert ids, "no skills discovered"
    assert not any("_preamble" in sid for sid in ids)


@pytest.mark.parametrize("name", sorted(_INLINE_PREAMBLE))
def test_inline_preamble_skills_share_identical_communication(name: str) -> None:
    blocks = {}
    for provider in PROVIDERS:
        text = (SKILLS_ROOT / provider / f"qc-{provider}-{name}.md").read_text(
            encoding="utf-8"
        )
        match = _COMMUNICATION_RE.search(text)
        assert match, f"{provider}/{name}: no Communication block"
        blocks[provider] = match.group(1)
    assert blocks["codex"] == blocks["claude"], name
    assert blocks["gemini"] == blocks["claude"], name


def test_biz_tech_usage_anchors_to_canonical_standard() -> None:
    for skill in discover_skills(SKILLS_ROOT, include_root=REPO_ROOT):
        if _BIZ_TECH_USAGE_RE.search(skill.body):
            assert "docs/standards/biz-tech-assertions.md" in skill.body, (
                f"{skill.skill_id} uses the BIZ/TECH descriptor but does not "
                f"reference docs/standards/biz-tech-assertions.md"
            )
