"""Test-result persistence + triage evidence (issue #292)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...atomic_io import atomic_write_json
from ...errors import UsageError
from ...events import EventLog
from ...gates import (
    GateFinding,
    GateResult,
    evaluate_final_gate,
    final_gate,
    merge_patch_if_approved,
    parse_gate_output,
    static_review_gate,
    write_abandon_artifact,
    write_gate_result,
)
from ...ids import ulid
from ...ids import run_id as new_run_id
from ...orchestrator import CURRENT_PHASE_ID, Orchestrator
from ...paths import RuntimePaths
from ...runtime.subprocess import run_command, scrub_provider_credentials
from ...security import resolve_repo_path
from ...storage.db import connect as _db_connect, transaction
from ...time_utils import now_iso



def _persist_test_results_and_evidence(
    *,
    paths: RuntimePaths,
    events: EventLog,
    run_id: str,
    failures: List[Dict[str, Any]],
    triage_items: List[Dict[str, Any]],
) -> None:
    """Insert one `test_results` row per failure plus an `evidence`
    row per linked artifact. Issue #103.

    Passes are skipped because `reports/last-run.json` only carries
    per-failure detail. The acceptance criterion is met for failed
    runs; green runs persist nothing into `test_results`, which is
    intentional and matches the schema's `failure_message` shape.
    """
    if not paths.db.exists():
        return
    if not failures:
        return
    db = _db_connect(paths.db)
    try:
        # Confirm the run row exists; the workflow above already
        # recorded it via `Orchestrator.record_run`.
        row = db.execute(
            "SELECT id FROM runs WHERE id = ? LIMIT 1;",
            (run_id,),
        ).fetchone()
        if row is None:
            return
        items_by_name = {item.get("name"): item for item in triage_items}
        ts = now_iso()
        with sqlite3.connect(":memory:"):
            pass  # placeholder — keep import locality consistent
        for failure in failures:
            scenario = str(failure.get("scenario") or "(unnamed)")
            classname = str(failure.get("classname") or failure.get("feature_uri") or "")
            line = failure.get("line")
            tags = [str(t) for t in (failure.get("tags") or [])]
            functional = next(
                (t for t in tags if t.startswith("@functional-")), None
            )
            lifecycle = next(
                (t for t in tags if t in {"@regression", "@smoke", "@nightly"}), None
            )
            tag_str = json.dumps(tags, ensure_ascii=False)
            test_id = ulid()
            db.execute(
                """
                INSERT INTO test_results(
                    id, run_id, scenario_name, feature_path, line, status,
                    duration_ms, functional_tag, lifecycle_tag, all_tags,
                    failure_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    test_id,
                    run_id,
                    scenario,
                    classname or "(unknown)",
                    int(line) if isinstance(line, int) else None,
                    "failed",
                    None,
                    functional or "@functional-uncategorized",
                    lifecycle,
                    tag_str,
                    str(failure.get("error_message") or ""),
                    ts,
                ),
            )
            # Evidence rows from the triage item's evidence list.
            item = items_by_name.get(scenario, {})
            for rel in item.get("evidence", []):
                target = paths.repo_root / rel
                kind = _classify_evidence_kind(rel)
                size = target.stat().st_size if target.is_file() else 0
                sha = _sha256_safe(target)
                evidence_id = ulid()
                db.execute(
                    """
                    INSERT INTO evidence(
                        id, run_id, kind, path, sha256, size_bytes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        evidence_id,
                        run_id,
                        kind,
                        rel,
                        sha,
                        size,
                        ts,
                    ),
                )
    finally:
        db.close()

def _classify_evidence_kind(rel: str) -> str:
    lower = rel.lower()
    if lower.endswith(".png") or lower.endswith(".jpg") or lower.endswith(".jpeg"):
        return "screenshot"
    if lower.endswith(".zip"):
        return "trace"
    if lower.endswith(".xml"):
        return "junit"
    if lower.endswith("summary.md") or lower.endswith("last-run.json"):
        return "summary"
    if "allure" in lower:
        return "allure"
    if "cucumber" in lower and lower.endswith(".html"):
        return "cucumber_html"
    if lower.endswith(".log"):
        return "log"
    if lower.endswith(".patch"):
        return "patch"
    return "other"

def _sha256_safe(target: Path) -> str:
    if not target.is_file():
        return ""
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return ""

def _triage_evidence(paths: RuntimePaths, failure: Dict[str, Any]) -> List[str]:
    evidence = ["reports/last-run.json"]
    summary = paths.repo_root / "reports" / "summary.md"
    if summary.exists():
        evidence.append("reports/summary.md")
    junit_xml = failure.get("junit_xml")
    if isinstance(junit_xml, str) and junit_xml:
        candidate = paths.repo_root / junit_xml
        if candidate.exists():
            evidence.append(junit_xml)
    # Issue #93 — collect Playwright screenshots/traces when the
    # report points at them (`screenshot`, `trace` fields) or when the
    # canonical Playwright `test-results/<scenario>/` directory exists
    # under `reports/playwright/`.
    for key in ("screenshot", "trace", "video"):
        rel = failure.get(key)
        if isinstance(rel, str) and rel and (paths.repo_root / rel).exists():
            evidence.append(rel)
    pw_dir = paths.repo_root / "reports" / "playwright"
    if pw_dir.is_dir():
        scenario_slug = re.sub(
            r"[^a-z0-9]+",
            "-",
            str(failure.get("scenario") or "").lower(),
        ).strip("-")
        if scenario_slug:
            for ext in ("zip", "png", "webm"):
                for path in pw_dir.rglob(f"*{scenario_slug}*.{ext}"):
                    rel = str(path.relative_to(paths.repo_root))
                    if rel not in evidence:
                        evidence.append(rel)
    return evidence

def _scenario_tag(failure: Dict[str, Any]) -> str:
    tags = [str(t) for t in (failure.get("tags") or [])]
    functional = next((t for t in tags if t.startswith("@functional-")), None)
    if functional:
        return functional
    scenario = str(failure.get("scenario") or "untagged-scenario").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", scenario).strip("-") or "untagged-scenario"
    return slug[:80]
