"""Final gate evaluation across pillars and violations.

Split from gates.py (issue #292).
"""
from __future__ import annotations

import sqlite3
from typing import List, Optional

from ..paths import RuntimePaths

from .pillars import _pillar_patch_resolution, _pillar_required_files, _tag_pillar
from .types import GateFinding, GateResult
from .violations import find_bug_evidence_violations, find_known_bug_policy_violations, find_run_report_violations, find_work_item_run_attestation_violations


def evaluate_final_gate(
    paths: RuntimePaths,
    *,
    conn: Optional[sqlite3.Connection] = None,
    work_item_id: Optional[str] = None,
) -> tuple[GateResult, dict[str, dict]]:
    """Run all final-gate pillars (issue #90).

    Returns the aggregated GateResult and a `pillars` map suitable for
    embedding in the run manifest so operators can see which pillar
    failed without parsing finding messages.
    """
    all_findings: List[GateFinding] = []
    pillars: dict[str, dict] = {}
    for name, check in _PILLAR_CHECKS:
        pillar_findings = check(paths)
        pillars[name] = {
            "status": "ok" if not pillar_findings else "failed",
            "findings_count": len(pillar_findings),
        }
        all_findings.extend(_tag_pillar(name, f) for f in pillar_findings)

    # Issue #184 — work-item attestation. Runs only when --work-item is
    # supplied; otherwise the pillar is reported as `skipped` so the
    # historic unscoped final-gate path keeps its semantics.
    if work_item_id is None:
        pillars["work_item_run_attestation"] = {
            "status": "skipped",
            "findings_count": 0,
        }
    else:
        attestation_findings = find_work_item_run_attestation_violations(
            paths, conn=conn, work_item_id=work_item_id
        )
        pillars["work_item_run_attestation"] = {
            "status": "ok" if not attestation_findings else "failed",
            "findings_count": len(attestation_findings),
        }
        all_findings.extend(
            _tag_pillar("work_item_run_attestation", f)
            for f in attestation_findings
        )

    if all_findings:
        return (
            GateResult(verdict="REJECT", reason="final_gate_failed", findings=all_findings),
            pillars,
        )
    return (
        GateResult(verdict="APPROVE", reason="final_gate_passed", findings=[]),
        pillars,
    )


def final_gate(paths: RuntimePaths) -> GateResult:
    result, _pillars = evaluate_final_gate(paths)
    return result


_PILLAR_CHECKS = (
    ("required_files", _pillar_required_files),
    ("patch_resolution", _pillar_patch_resolution),
    ("run_report", find_run_report_violations),
    ("bug_evidence", find_bug_evidence_violations),
    ("known_bug_policy", find_known_bug_policy_violations),
)
