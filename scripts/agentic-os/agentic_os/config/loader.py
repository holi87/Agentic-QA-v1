"""Config loader + active-override accessors (issue #292)."""
from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..errors import ConfigError, UsageError
from .types import AgenticConfig, DEFAULT_CONFIG_REL, LEGACY_CONFIG_REL
from .validators import _validate

try:  # PyYAML is the only runtime dependency expected on the contest laptop.
    import yaml  # type: ignore[import-untyped]
except ImportError as exc:  # pragma: no cover - environment-specific
    raise ConfigError(
        "PyYAML is required to load config/agentic-os.yml.\n"
        "install: python3 -m pip install --user pyyaml"
    ) from exc



def resolve_config_path(repo_root: Path) -> Path:
    """Return whichever config path exists. Prefer new `config/`, fall back to legacy."""
    new_path = repo_root / DEFAULT_CONFIG_REL
    legacy_path = repo_root / LEGACY_CONFIG_REL
    if new_path.exists():
        return new_path
    if legacy_path.exists():
        return legacy_path
    return new_path  # default destination for `init`

def load_config(path: Path) -> AgenticConfig:
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"invalid config {path}: top-level value must be a mapping")
    errors = _validate(raw)
    if errors:
        joined = "\n\n".join(errors)
        raise ConfigError(f"invalid config {path}\n{joined}")
    return AgenticConfig(raw=raw, source=path)
_ACTIVE_OVERRIDE: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "agentic_os_active_config_override",
    default=None,
)

def set_active_config_override(path: Path | None) -> None:
    _ACTIVE_OVERRIDE.set(path)

def get_active_config_override() -> Path | None:
    return _ACTIVE_OVERRIDE.get()

def load_or_default(repo_root: Path, override: Path | None = None) -> AgenticConfig:
    if override is None:
        override = _ACTIVE_OVERRIDE.get()
    path = override if override is not None else resolve_config_path(repo_root)
    return load_config(path)
