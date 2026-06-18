"""Coverage-pillar tagging and last-run loading helpers.

Split from gates.py (issue #292).
"""
from __future__ import annotations

from typing import List, Optional

from ..paths import RuntimePaths

from .patch_gate import find_patch_gate_violations
from .types import GateFinding


def _tag_pillar(pillar: str, finding: GateFinding) -> GateFinding:
    return GateFinding(
        path=finding.path,
        line=finding.line,
        message=f"[pillar={pillar}] {finding.message}",
    )


def _pillar_required_files(paths: RuntimePaths) -> List[GateFinding]:
    findings: List[GateFinding] = []
    for rel in FINAL_GATE_REQUIRED_FILES:
        target = paths.repo_root / rel
        if not target.exists():
            findings.append(GateFinding(rel, 1, "required Agentic OS file is missing"))
    run_tests = paths.repo_root / "run-tests.sh"
    if run_tests.exists() and (run_tests.stat().st_mode & 0o111) == 0:
        findings.append(GateFinding("run-tests.sh", 1, "runner must be executable"))
    return findings


def _pillar_patch_resolution(paths: RuntimePaths) -> List[GateFinding]:
    return list(find_patch_gate_violations(paths))


def _load_last_run(paths: RuntimePaths) -> tuple[Optional[dict], Optional[str]]:
    """Return (data, error) for `reports/last-run.json`."""
    target = paths.repo_root / "reports" / "last-run.json"
    if not target.is_file():
        return None, "missing reports/last-run.json"
    try:
        import json

        return json.loads(target.read_text(encoding="utf-8")), None
    except Exception as exc:
        return None, f"reports/last-run.json is not valid JSON: {exc}"


FINAL_GATE_REQUIRED_FILES: tuple[str, ...] = (
    "scripts/agentic-os.sh",
    "scripts/assertion-guard.py",
    "scripts/copy-reports.sh",
    "scripts/extract-last-run.sh",
    "scripts/build-summary.sh",
    "run-tests.sh",
    "config/agentic-os.yml.example",
)
