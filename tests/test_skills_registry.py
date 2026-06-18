"""Skills registry parsing, loading, discovery, ordering, and prompt composition."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from agentic_os.skills import (
    compose_prompt,
    discover_skills,
    is_external_host_allowed,
    list_skills,
    load_skill,
    load_skills_config,
    parse_frontmatter,
    validate_skill_body,
)


def _write_skill(root: Path, rel: str, name: str, body: str = "Body content") -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        textwrap.dedent(
            f"""\
            ---
            name: {name}
            description: Test skill for {name}.
            ---

            # Skill: {name}

            {body}
            """
        ),
        encoding="utf-8",
    )
    return target


def test_parse_frontmatter_extracts_meta() -> None:
    meta, body = parse_frontmatter("---\nname: x\ndescription: y\n---\nbody here\n")
    assert meta == {"name": "x", "description": "y"}
    assert body.strip() == "body here"


def test_parse_frontmatter_no_meta_returns_text_intact() -> None:
    meta, body = parse_frontmatter("just body\n")
    assert meta == {}
    assert "just body" in body


def test_validate_skill_body_rejects_literal_secret() -> None:
    with pytest.raises(ValueError):
        validate_skill_body("Set api_key=abcdef12345 here")


def test_load_skill_parses_file(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, "claude/example.md", "example")
    skill = load_skill(skills, "claude/example.md")
    assert skill.skill_id == "claude/example"
    assert skill.name == "example"
    assert skill.checksum
    assert skill.size_bytes > 0


def test_load_skill_blocks_path_traversal(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    with pytest.raises(ValueError):
        load_skill(skills, "../outside.md")


def test_load_skill_blocks_absolute_path(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    with pytest.raises(ValueError):
        load_skill(skills, "/etc/passwd")


def test_discover_skills_walks_subdirs(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, "claude/a.md", "a")
    _write_skill(skills, "codex/b.md", "b")
    _write_skill(skills, "gemini/c.md", "c")
    out = discover_skills(skills)
    ids = {s.skill_id for s in out}
    assert ids == {"claude/a", "codex/b", "gemini/c"}


def test_discover_skills_skips_readme(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "README.md").write_text("# readme\n", encoding="utf-8")
    (skills / "README_pl.md").write_text("# readme pl\n", encoding="utf-8")
    _write_skill(skills, "claude/x.md", "x")
    out = discover_skills(skills)
    assert [s.skill_id for s in out] == ["claude/x"]


def test_list_skills_returns_enabled_in_order(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, "claude/a.md", "a")
    _write_skill(skills, "claude/b.md", "b")
    config = {
        "skills": {
            "scope": "project",
            "project_dir": "skills",
            "global_dir": "~/.never",
            "per_role": {
                "planner": {"enabled": ["claude/b", "claude/a"], "disabled": []},
            },
        }
    }
    resolution = list_skills("planner", config=config, project_root=tmp_path)
    assert [s.skill_id for s in resolution.skills] == ["claude/b", "claude/a"]
    assert resolution.warnings == []


def test_list_skills_warns_on_missing(tmp_path: Path) -> None:
    config = {
        "skills": {
            "scope": "project",
            "project_dir": "skills",
            "global_dir": "~/.never",
            "per_role": {
                "planner": {"enabled": ["claude/ghost"], "disabled": []},
            },
        }
    }
    resolution = list_skills("planner", config=config, project_root=tmp_path)
    assert resolution.skills == []
    assert any("not found" in w for w in resolution.warnings)


def test_compose_prompt_prepends_skills(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, "claude/a.md", "alpha", body="Step 1: do A.")
    config = {
        "skills": {
            "scope": "project",
            "project_dir": "skills",
            "global_dir": "~/.never",
            "per_role": {"planner": {"enabled": ["claude/a"], "disabled": []}},
        }
    }
    final, resolution = compose_prompt(
        "planner", "Base task brief.", config=config, project_root=tmp_path
    )
    assert "Available skills" in final
    assert "Step 1: do A." in final
    assert final.endswith("Base task brief.")
    assert resolution.skills[0].name == "alpha"


def test_compose_prompt_no_skills_returns_base(tmp_path: Path) -> None:
    config = {
        "skills": {
            "scope": "project",
            "project_dir": "skills",
            "global_dir": "~/.never",
            "per_role": {"planner": {"enabled": [], "disabled": []}},
        }
    }
    final, resolution = compose_prompt(
        "planner", "Base task.", config=config, project_root=tmp_path
    )
    assert final == "Base task."
    assert resolution.skills == []


def test_load_skills_config_returns_defaults_when_missing(tmp_path: Path) -> None:
    cfg = load_skills_config(tmp_path / "missing.yml")
    assert cfg["skills"]["scope"] == "project"
    assert set(cfg["skills"]["per_role"]) == {"planner", "implementer", "reviewer", "triager"}


def test_load_skills_config_reads_file(tmp_path: Path) -> None:
    yml = tmp_path / "skills.yml"
    yml.write_text(
        yaml.safe_dump({"skills": {"scope": "global", "per_role": {"planner": {"enabled": ["x"]}}}}),
        encoding="utf-8",
    )
    cfg = load_skills_config(yml)
    assert cfg["skills"]["scope"] == "global"


def test_is_external_host_allowed_whitelist() -> None:
    assert is_external_host_allowed("https://github.com/user/repo")
    assert is_external_host_allowed("git@gitlab.com:user/repo.git")
    assert is_external_host_allowed("https://codeberg.org/user/repo")
    assert not is_external_host_allowed("https://evil.example.com/skills")


def test_list_skills_scope_both_project_wins(tmp_path: Path) -> None:
    project = tmp_path / "skills"
    global_dir = tmp_path / "global_skills"
    _write_skill(project, "claude/dup.md", "project-version", body="from project")
    _write_skill(global_dir, "claude/dup.md", "global-version", body="from global")
    config = {
        "skills": {
            "scope": "both",
            "project_dir": "skills",
            "global_dir": str(global_dir),
            "per_role": {"planner": {"enabled": ["claude/dup"], "disabled": []}},
        }
    }
    resolution = list_skills("planner", config=config, project_root=tmp_path)
    assert resolution.skills[0].name == "project-version"


def test_real_init_project_skills_reference_shipped_assets() -> None:
    root = Path(__file__).resolve().parents[1]
    for provider in ("claude", "codex", "gemini"):
        path = root / "skills" / provider / f"qc-{provider}-implementer-init-project.md"
        text = path.read_text(encoding="utf-8")
        assert "docs/standards/" in text, path
        assert "config/prompts/" in text, path
        assert "scripts/agentic-os.sh" in text, path
        assert "needs_input: test_stack" in text, path
        assert "codex-skills" not in text, path
        assert "template/" not in text, path
        assert "$HOME/Desktop/GenAI/hackaton" not in text, path


def test_real_planner_and_implementer_skills_enforce_candidate_quality_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    for provider in ("claude", "codex", "gemini"):
        planner = (root / "skills" / provider / f"qc-{provider}-planner-analyze-task.md").read_text(encoding="utf-8")
        api = (root / "skills" / provider / f"qc-{provider}-implementer-implement-api.md").read_text(encoding="utf-8")
        ui = (root / "skills" / provider / f"qc-{provider}-implementer-implement-ui.md").read_text(encoding="utf-8")
        reviewer = (root / "skills" / provider / f"qc-{provider}-reviewer-validate-tests.md").read_text(encoding="utf-8")
        assert "Candidate Quality Contract" in planner
        assert "visibility-only, status-only" in planner
        assert "generate_now" in api
        assert "needs_input: candidate_metadata" in api
        assert "generate_now" in ui
        assert "needs_input: candidate_metadata" in ui
        assert "Coverage depth gate" in reviewer
