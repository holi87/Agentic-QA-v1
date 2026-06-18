"""Dry-run pipeline stage (issue #292)."""
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
from ._types import WorkflowResult



MANIFEST_SCHEMA_VERSION = 1
_REPORT_SOURCE_ARTIFACTS = (
    "build/test-results/test",
    "build/reports/tests/test",
    "build/reports/cucumber",
    "build/reports/allure-report",
    "test-results",
    "playwright-report",
    "playwright-report.json",
)

def env_hash() -> str:
    keys = sorted(k for k in os.environ.keys() if not k.startswith(("CLAUDE", "ANTHROPIC", "OPENAI")))
    blob = "\n".join(f"{k}={os.environ[k]}" for k in keys)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
_env_hash = env_hash

def _clean_report_source_artifacts(paths: RuntimePaths, events: EventLog) -> None:
    """Remove stale report inputs before a new runner execution.

    `copy-reports.sh --clean` refreshes `reports/`, but the extractor still
    reads source locations such as `build/test-results/test` and
    `playwright-report.json`. Leaving those in place can merge an old JUnit run
    with the current Playwright run and overstate the result counts.
    """
    removed: List[str] = []
    for rel in _REPORT_SOURCE_ARTIFACTS:
        target = paths.repo_root / rel
        if not target.exists() and not target.is_symlink():
            continue
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
        removed.append(rel)
    if removed:
        events.write(
            "reports.source_artifacts_cleaned",
            severity="info",
            payload={"removed": removed},
        )

def write_manifest(
    *,
    paths: RuntimePaths,
    run_id_str: str,
    task_id: str,
    kind: str,
    command: List[str],
    cwd: str,
    started_at: str,
    finished_at: str,
    exit_code: int,
    failure_kind: Optional[str],
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    evidence_dir = paths.evidence_dir / run_id_str
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = evidence_dir / "manifest.json"
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id_str,
        "task_id": task_id,
        "phase_id": CURRENT_PHASE_ID,
        "kind": kind,
        "command": command,
        "cwd": cwd,
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": exit_code,
        "failure_kind": failure_kind,
        "sut": {
            "git_sha": "unknown",
            "compose_project": None,
            "docker_images": [],
        },
        "artifacts": [],
    }
    if extra:
        manifest.update(extra)
    atomic_write_json(manifest_path, manifest, trailing_newline=False)
    return manifest_path
_write_manifest = write_manifest

def run_dry_run(
    orchestrator: Orchestrator,
    paths: RuntimePaths,
    events: EventLog,
    *,
    fake_sut: bool,
) -> WorkflowResult:
    # Issue #73 — `--fake-sut` is the official onboarding proof fixture.
    # It seeds a passing `reports/last-run.json` + `reports/summary.md`
    # so the operator can run doctor → run dry-run --fake-sut → status
    # without provisioning an external SUT. The run is marked
    # `discovery_only=true` so issue #100's zero-test guard accepts it.
    if fake_sut:
        return _run_fake_sut(orchestrator, paths, events)

    from ...runner import run_and_record

    orchestrator.seed_phases()
    task_id = orchestrator.create_task(
        phase_id=CURRENT_PHASE_ID,
        kind="run",
        payload={"workflow": "dry-run", "resume_allowed": True},
    )
    orchestrator.lease_task(task_id, owner="orchestrator", ttl_seconds=60)
    orchestrator.mark_running(task_id, owner="orchestrator")

    command = [
        sys.executable,
        "-c",
        "import sys; print('dry-run: safe subprocess executed'); print('dry-run: stderr captured', file=sys.stderr)",
    ]
    record = run_and_record(
        orchestrator=orchestrator,
        paths=paths,
        events=events,
        task_id=task_id,
        kind="dry-run",
        command=command,
        timeout_seconds=10,
        shutdown_grace_seconds=2,
    )
    result = record.result
    task_status = "succeeded" if result.exit_code == 0 else "failed"
    orchestrator.finish_task(
        task_id,
        status=task_status,
        exit_code=result.exit_code,
        error_class=result.failure_kind,
    )

    return WorkflowResult(
        ok=result.exit_code == 0,
        exit_code=result.exit_code,
        failure_kind=result.failure_kind,
        task_id=task_id,
        run_id=record.run_id,
        manifest_path=str(record.manifest_path.relative_to(paths.repo_root)),
        reports_path=None,
        bugs_opened=[],
    )

def _run_fake_sut(
    orchestrator: Orchestrator,
    paths: RuntimePaths,
    events: EventLog,
) -> WorkflowResult:
    """Issue #73 — emit a deterministic discovery_only report and
    register it as a green dry-run. Lets `doctor → run dry-run --fake-sut
    → status` work on a fresh checkout without an external SUT.
    """
    orchestrator.seed_phases()
    task_id = orchestrator.create_task(
        phase_id=CURRENT_PHASE_ID,
        kind="run",
        payload={"workflow": "dry-run", "fake_sut": True, "resume_allowed": False},
    )
    orchestrator.lease_task(task_id, owner="orchestrator", ttl_seconds=60)
    orchestrator.mark_running(task_id, owner="orchestrator")

    started_at = now_iso()
    run_id_str = new_run_id()
    reports = paths.repo_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        reports / "last-run.json",
        {
            "ran_at": started_at,
            "total": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "failures": [],
            "discovery_only": True,
            "source": "agentic-os fake SUT (issue #73)",
        },
        trailing_newline=False,
    )
    (reports / "summary.md").write_text(
        "# Fake SUT dry-run\n\n"
        "Generated by `agentic-os run dry-run --fake-sut` (issue #73).\n"
        "No real tests executed — this fixture proves doctor → run → "
        "status works on a fresh checkout.\n",
        encoding="utf-8",
    )
    log_path = paths.subprocess_logs_dir / f"{run_id_str}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("fake-sut: no real subprocess executed\n", encoding="utf-8")
    orchestrator.record_run(
        task_id=task_id,
        run_id=run_id_str,
        command=["agentic-os", "run", "dry-run", "--fake-sut"],
        cwd=str(paths.repo_root),
        env_hash=env_hash(),
        log_path=str(log_path.relative_to(paths.repo_root)),
        started_at=started_at,
    )
    finished_at = now_iso()
    manifest_path = write_manifest(
        paths=paths,
        run_id_str=run_id_str,
        task_id=task_id,
        kind="dry-run",
        command=["agentic-os", "run", "dry-run", "--fake-sut"],
        cwd=str(paths.repo_root),
        started_at=started_at,
        finished_at=finished_at,
        exit_code=0,
        failure_kind=None,
        extra={
            "fake_sut": True,
            "reports_path": "reports",
            "reason": "issue #73 fake-SUT proof fixture",
        },
    )
    orchestrator.finish_run(
        run_id=run_id_str,
        exit_code=0,
        duration_ms=0,
        failure_kind=None,
        unmapped_exit=False,
        evidence_path=str((paths.evidence_dir / run_id_str).relative_to(paths.repo_root)),
        manifest_path=str(manifest_path.relative_to(paths.repo_root)),
        finished_at=finished_at,
    )
    orchestrator.finish_task(task_id, status="succeeded", exit_code=0)
    events.write(
        "run.fake_sut_completed",
        payload={"run_id": run_id_str, "task_id": task_id},
    )
    return WorkflowResult(
        ok=True,
        exit_code=0,
        failure_kind=None,
        task_id=task_id,
        run_id=run_id_str,
        manifest_path=str(manifest_path.relative_to(paths.repo_root)),
        reports_path="reports",
        bugs_opened=[],
    )

def _summarize_model_roles_for_manifest(paths: RuntimePaths) -> Dict[str, Any]:
    """Issue #101 — list configured model roles and whether their
    binaries are reachable. Honest about which roles are wired vs.
    just declared so the dashboard/CLI can surface the gap.
    """
    import shutil

    out: Dict[str, Any] = {}
    try:
        from ...config import load_or_default

        cfg = load_or_default(paths.repo_root)
    except Exception:
        return {"error": "config_load_failed"}
    models = cfg.raw.get("models") or {}
    for role, role_cfg in models.items():
        if not isinstance(role_cfg, dict):
            continue
        command = role_cfg.get("command") or []
        binary = command[0] if command else None
        out[role] = {
            "provider": role_cfg.get("provider"),
            "role": role_cfg.get("role"),
            "binary": binary,
            "binary_on_path": bool(binary and shutil.which(binary)),
            "auto_fire": bool(role_cfg.get("auto_fire")),
        }
    return out

def _augment_manifest(path: Path, extra: Dict[str, Any]) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update(extra)
    atomic_write_json(path, manifest, trailing_newline=False)
