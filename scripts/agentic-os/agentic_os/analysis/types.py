"""Shared analysis data container.

Split from analysis.py (issue #292).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class _AnalysisInputs:
    work_item: Dict[str, Any]
    spec_markdown: str
    config_snapshot: Dict[str, Any]
    config_warning: Optional[str]
