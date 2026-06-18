"""Config writer + secret redaction extracted from config.py (issue #292)."""
from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..errors import ConfigError, UsageError
from .validators import _validate

try:  # PyYAML is the only runtime dependency expected on the contest laptop.
    import yaml  # type: ignore[import-untyped]
except ImportError as exc:  # pragma: no cover - environment-specific
    raise ConfigError(
        "PyYAML is required to load config/agentic-os.yml.\n"
        "install: python3 -m pip install --user pyyaml"
    ) from exc



def redact_secrets(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deep copy of the config with credential values masked.

    Used by GET /api/config so the dashboard never receives the literal
    credentials value. Env names/file paths are references, not secrets — they
    are kept so the operator can see what is wired up.
    """
    import copy

    safe = copy.deepcopy(raw)
    sut = safe.get("sut")
    if isinstance(sut, dict):
        creds = sut.get("credentials")
        if isinstance(creds, dict):
            ref_type = creds.get("ref_type")
            value = creds.get("value")
            if ref_type == "env" and isinstance(value, str) and value:
                creds["value"] = f"env:{value}"
            elif ref_type == "file" and isinstance(value, str) and value:
                creds["value"] = f"file:{value}"
            elif ref_type == "none":
                creds["value"] = None
    return safe

def write_config(path: Path, raw: Dict[str, Any]) -> None:
    """Validate then write config to disk atomically."""
    if not isinstance(raw, dict):
        raise ConfigError("config must be a mapping")
    errors = _validate(raw)
    if errors:
        joined = "\n\n".join(errors)
        raise ConfigError(f"invalid config payload\n{joined}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(raw, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    tmp.replace(path)
