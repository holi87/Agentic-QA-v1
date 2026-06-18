"""Run-report / bug-evidence / known-bug / attestation violation finders.

Split from gates.py (issue #292).
"""
from __future__ import annotations

import re
import sqlite3
from typing import List, Optional

from ..paths import RuntimePaths
from ..storage.db import connect as _db_connect

from .pillars import _load_last_run
from .static_review import _KNOWN_BUG_RE
from .types import GateFinding


def find_run_report_violations(paths: RuntimePaths) -> List[GateFinding]:
    """Pillar: latest run report must exist and look finalized.

    Final gate cannot certify readiness without a recent test run on
    disk. Required artifacts: `reports/last-run.json` (valid JSON with
    `total`/`passed`/`failed`/`skipped` numeric fields) and
    `reports/summary.md`.
    """
    data, err = _load_last_run(paths)
    if err is not None:
        return [GateFinding("reports/last-run.json", 1, err)]
    summary = paths.repo_root / "reports" / "summary.md"
    findings: List[GateFinding] = []
    if not summary.is_file():
        findings.append(GateFinding("reports/summary.md", 1, "summary report is missing"))
    if not isinstance(data, dict):
        findings.append(
            GateFinding("reports/last-run.json", 1, "report root must be a JSON object")
        )
        return findings
    for key in ("total", "passed", "failed", "skipped"):
        if not isinstance(data.get(key), int):
            findings.append(
                GateFinding(
                    "reports/last-run.json",
                    1,
                    f"report field '{key}' must be an integer",
                )
            )
    # Issue #100 — `total=0` without an explicit dry-run/discovery flag
    # is a silent infra failure, not a green run. The escape hatch must
    # be an explicit boolean `true` (codex review on #129): truthy
    # strings like `"false"` or numbers must NOT bypass the block.
    if isinstance(data.get("total"), int) and data["total"] == 0:
        discovery_marker = data.get("discovery_only") is True or data.get("dry_run") is True
        if not discovery_marker:
            findings.append(
                GateFinding(
                    "reports/last-run.json",
                    1,
                    "zero tests collected — set `discovery_only: true` if intentional",
                )
            )
    return findings


def find_bug_evidence_violations(paths: RuntimePaths) -> List[GateFinding]:
    """Pillar: product failures must have a registered bug file.

    A failure without `@known-bug` is a product red. Each such failure
    must point at a bug file under `bugs/` so triage evidence is
    auditable. Missing-report case is handled by `find_run_report_violations`.
    """
    data, err = _load_last_run(paths)
    if err is not None or not isinstance(data, dict):
        return []
    failures = data.get("failures") or []
    if not isinstance(failures, list):
        return [GateFinding("reports/last-run.json", 1, "failures must be a list")]
    findings: List[GateFinding] = []
    bugs_dir = paths.repo_root / "bugs"
    for idx, failure in enumerate(failures):
        if not isinstance(failure, dict):
            continue
        tags = failure.get("tags") or []
        if not isinstance(tags, list):
            continue
        is_known_bug = any(
            isinstance(t, str) and _KNOWN_BUG_RE.search(t) for t in tags
        )
        if is_known_bug:
            continue
        # Product failure — there must be a bug-NNN tag or a bug record
        # somewhere under `bugs/`. Either route counts as evidence.
        has_bug_tag = any(
            isinstance(t, str) and _BUG_FILE_RE.search(t) for t in tags
        )
        if has_bug_tag:
            continue
        scenario = failure.get("scenario") or f"failure[{idx}]"
        bug_files = list(bugs_dir.glob("BUG-*.md")) if bugs_dir.is_dir() else []
        if not bug_files:
            findings.append(
                GateFinding(
                    "reports/last-run.json",
                    1,
                    f"product failure '{scenario}' has no bug record under bugs/",
                )
            )
    return findings


def find_known_bug_policy_violations(paths: RuntimePaths) -> List[GateFinding]:
    """Pillar: every `@bug-NNN` tag in the last run must point at a real bug file.

    Lightweight check: scans the bug id from the tag and looks for a file
    matching `bugs/BUG-NNN-*.md`. Deeper triage-evidence enforcement is
    issue #110.
    """
    data, err = _load_last_run(paths)
    if err is not None or not isinstance(data, dict):
        return []
    failures = data.get("failures") or []
    if not isinstance(failures, list):
        return []
    bugs_dir = paths.repo_root / "bugs"
    findings: List[GateFinding] = []
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        tags = failure.get("tags") or []
        if not isinstance(tags, list):
            continue
        for tag in tags:
            if not isinstance(tag, str):
                continue
            match = _BUG_FILE_RE.search(tag)
            if match is None:
                continue
            bug_id = match.group(1)
            candidates = list(bugs_dir.glob(f"BUG-{bug_id}-*.md")) if bugs_dir.is_dir() else []
            if not candidates:
                findings.append(
                    GateFinding(
                        "reports/last-run.json",
                        1,
                        f"tag @bug-{bug_id} has no matching bugs/BUG-{bug_id}-*.md file",
                    )
                )
    return findings


def find_work_item_run_attestation_violations(
    paths: RuntimePaths,
    *,
    conn: Optional[sqlite3.Connection] = None,
    work_item_id: Optional[str] = None,
) -> List[GateFinding]:
    """Pillar: a work-item-scoped final gate needs a real run for that
    work item (issue #184).

    Without this check, `final-gate --work-item X` happily accepted the
    global `reports/last-run.json` even when `run-tests --work-item X`
    had never executed. We require at least one `kind='run'` artifact
    registered against the work item; the run-tests workflow attaches
    this artifact in `_attach_run_artifacts_to_work_item`.
    """

    if work_item_id is None:
        return []
    own_conn = False
    if conn is None:
        try:
            conn = _db_connect(paths.db)
            own_conn = True
        except Exception as exc:
            return [
                GateFinding(
                    "agentic-os-runtime/runtime.db",
                    1,
                    f"cannot open runtime DB to verify work-item attestation: {exc}",
                )
            ]
    try:
        cur = conn.execute(
            "SELECT path FROM work_item_artifacts "
            " WHERE work_item_id=? AND kind='run'",
            (work_item_id,),
        )
        run_paths: List[str] = [str(row[0]) for row in cur.fetchall()]
    finally:
        if own_conn:
            conn.close()

    # `_attach_run_artifacts_to_work_item` writes every workflow's run
    # row with the literal artifact kind `'run'` (run-tests, final-gate,
    # etc. — all collide). Inspect the manifest payload itself and
    # require at least one to be an actual `run-tests` execution,
    # otherwise final-gate could rubber-stamp its own prior invocation
    # or any unrelated workflow registered against the work item.
    import json

    has_real_run_tests = False
    for rel in run_paths:
        manifest_path = paths.repo_root / rel
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):
            continue
        if manifest.get("kind") == "run-tests":
            has_real_run_tests = True
            break

    if not has_real_run_tests:
        return [
            GateFinding(
                "reports/last-run.json",
                1,
                (
                    f"no run-tests run registered for work item "
                    f"{work_item_id!r}; run `agentic-os run run-tests "
                    f"--work-item {work_item_id}` (or supply an explicit "
                    "waiver) before requesting final-gate"
                ),
            )
        ]
    return []


_BUG_FILE_RE = re.compile(r"@bug-(\d{3,})\b", re.IGNORECASE)
