"""Shared CLI state — the active config override dict (issue #292).

Lives in a tiny module so every `cli.cmd_*` submodule can read it without a
circular import back into `cli/__init__.py`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional


# Issue #77 — global `--config <path>` override threaded into every command.
_ACTIVE_CONFIG_OVERRIDE: Dict[str, Any] = {"path": None}


def _active_config_override() -> Optional[Path]:
    """Return the path set by `main()` via `--config <path>`, if any."""
    return _ACTIVE_CONFIG_OVERRIDE.get("path")
