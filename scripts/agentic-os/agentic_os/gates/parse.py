"""Parse structured gate output into a GateResult.

Split from gates.py (issue #292).
"""
from __future__ import annotations

import re
from typing import List

from .types import GateFinding, GateResult


def parse_gate_output(text: str) -> GateResult:
    """Parse strict Codex reviewer output."""
    lines = [line.rstrip() for line in text.splitlines()]
    if len(lines) < 4:
        raise ValueError("gate output is too short")
    verdict_line = _value_line(lines[0], "verdict")
    reason = _value_line(lines[1], "reason")
    if verdict_line not in {"APPROVE", "REJECT"}:
        raise ValueError(f"invalid gate verdict: {verdict_line!r}")
    if not reason:
        raise ValueError("gate reason is required")
    try:
        findings_idx = lines.index("findings:")
    except ValueError as exc:
        raise ValueError("gate output missing findings block") from exc
    ready_indices = [idx for idx, line in enumerate(lines) if line.strip() == "READY"]
    if not ready_indices:
        raise ValueError("gate output must contain a READY terminator")
    ready_idx = ready_indices[0]
    if ready_idx <= findings_idx:
        raise ValueError("gate output READY appears before findings")
    if sum(1 for line in lines if line.startswith("verdict:")) != 1:
        raise ValueError("gate output must contain exactly one verdict")
    if sum(1 for line in lines if line.startswith("reason:")) != 1:
        raise ValueError("gate output must contain exactly one reason")
    findings: List[GateFinding] = []
    for line in lines[findings_idx + 1:ready_idx]:
        if not line.strip():
            continue
        match = _FINDING_RE.match(line)
        if not match:
            raise ValueError(f"malformed finding line: {line!r}")
        findings.append(
            GateFinding(path=match.group(1), line=int(match.group(2)), message=match.group(3))
        )
    return GateResult(verdict=verdict_line, reason=reason, findings=findings, raw_output=text)


def _value_line(line: str, key: str) -> str:
    prefix = f"{key}:"
    if not line.startswith(prefix):
        raise ValueError(f"gate output must start with {prefix}")
    return line[len(prefix):].strip()


_FINDING_RE = re.compile(r"^-\s+(.+?):(\d+)\s+(?:\u2014|-)\s+(.+)$")
