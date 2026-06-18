"""skills registry + per-agent prompt injection.

Skille to opcjonalne kawalki promptu wybierane przez operatora per
role. Modul wystawia:

- `load_skill(path)` parsuje YAML frontmatter + body.
- `list_skills(role, *, scope, project_root, global_root)` zwraca enabled
  skille z `config/skills.yml`.
- `compose_prompt(role, base_prompt, ...)` dokleja zawartosc skilli na
  poczatek promptu.
- `validate_skill_body(text)` blokuje literal sekrety + path traversal
  ID.

Wszystkie skladniki sa pure functions (poza I/O odczytem plikow); brak
side effectow w runtime.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import yaml  # type: ignore[import-untyped]
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("PyYAML required for skills loader") from exc


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_SECRET_RE = re.compile(
    r"(?i)(bearer|api[_-]?key|token|secret|password)\s*[:=]\s*[A-Za-z0-9_\-]{6,}"
)
_VALID_ROLES = ("planner", "implementer", "reviewer", "triager")
_ALLOWED_HOST_PATTERNS = (
    "github.com",
    "gitlab.com",
    "codeberg.org",
)
_DOC_FILENAMES = {"README.md", "README_pl.md"}

# Splice directives resolved at load time. The on-disk skill body stays
# deduplicated (humans edit one source); the composed prompt the model
# sees is the fully-inlined text. See docs/standards/ + skills/_preamble_*.
_PREAMBLE_RE = re.compile(r"\$\{include_preamble\}")
_INCLUDE_RE = re.compile(r"\$\{include:\s*([^}]+?)\s*\}")


@dataclass(frozen=True)
class Skill:
    skill_id: str            # 'claude/QC-claude-analyze-task'
    name: str
    description: str
    body: str                # Markdown content po frontmatter
    source_path: str         # absolute path
    source: str              # 'project' | 'global' | 'external'
    checksum: str            # sha256 hex
    size_bytes: int
    tags: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkillsResolution:
    role: str
    skills: List[Skill]
    warnings: List[str] = field(default_factory=list)


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Parse `---YAML---body` skill format. Returns ({}, text) when no frontmatter."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a mapping")
    return meta, match.group(2)


def validate_skill_body(text: str) -> None:
    """Reject skill content with literal-looking secrets."""
    if _SECRET_RE.search(text):
        raise ValueError("skill body contains literal secret pattern; use env-ref instead")


def _safe_skill_id(rel_path: str) -> str:
    """Path traversal check + canonical form."""
    parts = rel_path.replace("\\", "/").split("/")
    if any(p in ("", "..", ".") for p in parts):
        raise ValueError(f"unsafe skill id: {rel_path!r}")
    if rel_path.startswith("/"):
        raise ValueError(f"skill id must be relative: {rel_path!r}")
    if not rel_path.endswith(".md"):
        raise ValueError(f"skill file must end with .md: {rel_path!r}")
    return rel_path[:-3]  # drop .md


def resolve_skill_includes(
    body: str,
    skill_id: str,
    skills_root: Path,
    include_root: Path,
) -> str:
    """Splice `${include_preamble}` + `${include: <path>}` directives inline.

    Included content is stripped of surrounding whitespace so a directive on
    its own line reproduces the previously-inlined block byte-for-byte. All
    include paths must resolve inside `include_root` (path-traversal guard).
    """
    base = include_root.resolve()

    def _read(path: Path, what: str) -> str:
        resolved = path.resolve()
        try:
            resolved.relative_to(base)
        except ValueError as exc:
            raise ValueError(f"{what} escapes include root: {path}") from exc
        if not resolved.is_file():
            raise ValueError(f"{what} not found: {path}")
        return resolved.read_text(encoding="utf-8").strip()

    def _preamble(_match: "re.Match[str]") -> str:
        provider = skill_id.split("/", 1)[0]
        return _read(skills_root / f"_preamble_{provider}.md", "preamble")

    def _include(match: "re.Match[str]") -> str:
        return _read(base / match.group(1).strip(), "include")

    # Function replacements never interpret regex backrefs in the result.
    body = _PREAMBLE_RE.sub(_preamble, body)
    body = _INCLUDE_RE.sub(_include, body)
    return body


def load_skill(
    skills_root: Path,
    rel_path: str,
    *,
    source: str = "project",
    include_root: Optional[Path] = None,
) -> Skill:
    """Read a single skill file from disk, resolving splice directives."""
    skill_id = _safe_skill_id(rel_path)
    target = (skills_root / rel_path).resolve()
    if not target.is_file():
        raise ValueError(f"skill file not found: {rel_path}")
    try:
        target.relative_to(skills_root.resolve())
    except ValueError as exc:
        raise ValueError(f"skill resolves outside skills root: {rel_path!r}") from exc
    raw = target.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    validate_skill_body(text)
    meta, body = parse_frontmatter(text)
    body = resolve_skill_includes(
        body, skill_id, skills_root, include_root or skills_root.parent
    )
    # Re-validate AFTER splice: an `${include: ...}` directive can pull text
    # from outside the skill file (e.g. a .env-style file under include_root),
    # so the secret guard must also see the fully-expanded body.
    validate_skill_body(body)
    name = str(meta.get("name") or skill_id.rsplit("/", 1)[-1])
    description = str(meta.get("description") or "").strip()
    tags = meta.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    return Skill(
        skill_id=skill_id,
        name=name,
        description=description,
        body=body.strip() + "\n",
        source_path=str(target),
        source=source,
        checksum=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        tags=[str(t) for t in tags],
    )


def discover_skills(
    skills_root: Path,
    *,
    source: str = "project",
    include_root: Optional[Path] = None,
) -> List[Skill]:
    """Walk a skills root and return all valid `.md` files as Skill objects.

    Files starting with `_` are splice fragments (e.g. `_preamble_*.md`), not
    skills — they are skipped.
    """
    if not skills_root.exists():
        return []
    root = skills_root.resolve()
    out: List[Skill] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            if not name.endswith(".md") or name in _DOC_FILENAMES or name.startswith("_"):
                continue
            abs_path = Path(dirpath) / name
            try:
                rel = abs_path.relative_to(root).as_posix()
            except ValueError:
                continue
            try:
                out.append(load_skill(root, rel, source=source, include_root=include_root))
            except ValueError:
                continue  # skip malformed
    return sorted(out, key=lambda s: s.skill_id)


def load_skills_config(config_path: Path) -> Dict[str, Any]:
    """Load `config/skills.yml` with sane defaults when missing."""
    if not config_path.exists():
        return {
            "version": "1.0",
            "skills": {
                "scope": "project",
                "project_dir": "skills",
                "global_dir": "~/agentic-os-skills",
                "per_role": {role: {"enabled": [], "disabled": []} for role in _VALID_ROLES},
            },
        }
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid skills.yml: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("skills.yml top-level must be a mapping")
    return data


def list_skills(
    role: str,
    *,
    config: Dict[str, Any],
    project_root: Path,
    provider: Optional[str] = None,
) -> SkillsResolution:
    """Return enabled skills for a role, merged per `scope`.

    When `provider` is given, only skill IDs starting with `{provider}/`
    are kept (cross-provider skills are filtered out silently). This
    lets `config/skills.yml` enable all 3 providers up-front without
    spamming warnings for the inactive ones.
    """
    if role not in _VALID_ROLES:
        raise ValueError(f"unknown role: {role!r}")
    skills_block = (config.get("skills") or {})
    scope = skills_block.get("scope", "project")
    project_dir = skills_block.get("project_dir") or "skills"
    global_dir = os.path.expanduser(skills_block.get("global_dir") or "~/agentic-os-skills")
    per_role = (skills_block.get("per_role") or {}).get(role, {}) or {}
    enabled_ids = [str(x) for x in (per_role.get("enabled") or [])]
    warnings: List[str] = []

    if provider:
        prefix = f"{provider}/"
        enabled_ids = [sid for sid in enabled_ids if sid.startswith(prefix)]

    available: Dict[str, Skill] = {}
    if scope in ("global", "both"):
        global_path = Path(global_dir)
        for skill in discover_skills(
            global_path, source="global", include_root=global_path
        ):
            available[skill.skill_id] = skill
    if scope in ("project", "both"):
        # Project wygrywa przy konflikcie nazwy.
        for skill in discover_skills(
            project_root / project_dir, source="project", include_root=project_root
        ):
            available[skill.skill_id] = skill

    selected: List[Skill] = []
    for sid in enabled_ids:
        skill = available.get(sid)
        if skill is None:
            warnings.append(f"enabled skill not found: {sid}")
            continue
        selected.append(skill)
    return SkillsResolution(role=role, skills=selected, warnings=warnings)


def compose_prompt(
    role: str,
    base_prompt: str,
    *,
    config: Dict[str, Any],
    project_root: Path,
    provider: Optional[str] = None,
) -> Tuple[str, SkillsResolution]:
    """Return (final_prompt, resolution) with skills prepended."""
    resolution = list_skills(
        role, config=config, project_root=project_root, provider=provider
    )
    if not resolution.skills:
        return base_prompt, resolution
    blocks = ["## Available skills\n"]
    blocks.append(
        "You have the following skills loaded for this invocation. Read each "
        "once, then apply when relevant. Do NOT re-summarize them in output.\n"
    )
    for skill in resolution.skills:
        blocks.append(f"### Skill: {skill.name}\n\n{skill.body.strip()}\n")
    blocks.append("---\n")
    blocks.append(base_prompt)
    return "\n".join(blocks), resolution


def is_external_host_allowed(url: str) -> bool:
    """Whitelist host for skills install endpoint."""
    return any(host in url for host in _ALLOWED_HOST_PATTERNS)
