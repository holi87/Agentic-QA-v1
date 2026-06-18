"""Gate result dataclasses (findings, verdicts, patch-merge outcome).

Split from gates.py (issue #292).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass(frozen=True)
class GateFinding:
    path: str
    line: int
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line} - {self.message}"


@dataclass(frozen=True)
class GateResult:
    verdict: str
    reason: str
    findings: List[GateFinding]
    raw_output: str = ""

    @property
    def approved(self) -> bool:
        return self.verdict == "APPROVE"

    def to_text(self) -> str:
        lines = [
            f"verdict: {self.verdict}",
            f"reason: {self.reason}",
            "",
            "findings:",
        ]
        if self.findings:
            lines.extend(f"- {finding.render()}" for finding in self.findings)
        else:
            lines.append("- OK:1 - no blocking findings")
        lines.append("READY")
        return "\n".join(lines) + "\n"

    def to_json(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "approved": self.approved,
            "findings": [
                {"path": f.path, "line": f.line, "message": f.message}
                for f in self.findings
            ],
        }


@dataclass(frozen=True)
class PatchMergeResult:
    applied: bool
    blocked: bool
    log_path: Optional[Path]
