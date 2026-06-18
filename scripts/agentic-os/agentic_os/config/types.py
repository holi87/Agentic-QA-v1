"""Dataclasses + path constants extracted from config.py (issue #292)."""
from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..errors import ConfigError, UsageError



DEFAULT_CONFIG_REL = "config/agentic-os.yml"
LEGACY_CONFIG_REL = ".qualitycat/agentic-os.yml"

@dataclass(frozen=True)
class AgenticConfig:
    raw: Dict[str, Any]
    source: Path

    @property
    def runtime_root(self) -> str:
        return self.raw["runtime"]["root"]

    @property
    def dashboard_host(self) -> str:
        return self.raw["dashboard"]["host"]

    @property
    def dashboard_port(self) -> int:
        return int(self.raw["dashboard"]["port"])

@dataclass
class _ValidationCtx:
    errors: List[str] = field(default_factory=list)

    def fail(self, path: str, expected: str, actual: Any) -> None:
        self.errors.append(f"path: {path}\nexpected: {expected}\nactual: {actual!r}")
