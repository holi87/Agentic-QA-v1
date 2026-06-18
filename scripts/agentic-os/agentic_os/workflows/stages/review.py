"""Review-gate pipeline stage (issue #292)."""
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
from .attachments import _attach_gate_to_work_item, _read_diff, _register_apply_artifact
from .dry_run import env_hash, write_manifest
from .leases import _acquire_reviewer_lease, _release_reviewer_lease, _review_gate_busy_result



_SKILL_FAILURE_REJECT_THRESHOLD = 2

def _record_skill_failure_on_persistent_reject(
    conn: sqlite3.Connection, *, scope: str, reason: Optional[str]
) -> None:
    """Issue #287 — record a `skill_failure` when a reviewer keeps REJECTing.

    Reads the recent `gate.review_finished` events for this `scope` (already
    written, current one included) and counts the consecutive REJECTs ending
    with the most recent. When the streak reaches the threshold, the failing
    reviewer-skill family (keyed by scope) is recorded with the clustered
    reject reason. Wholly best-effort: any failure is swallowed so a bad hint
    can never change the gate result or the WorkflowResult.
    """
    try:
        rows = conn.execute(
            "SELECT payload FROM events WHERE kind='gate.review_finished' "
            "AND json_extract(payload, '$.scope') = ? "
            "ORDER BY ts DESC, id DESC LIMIT 20;",
            (scope,),
        ).fetchall()
        consecutive = 0
        for row in rows:
            payload_raw = row["payload"] if hasattr(row, "keys") else row[0]
            try:
                payload = json.loads(payload_raw) if isinstance(payload_raw, str) else {}
            except (ValueError, TypeError):
                break
            if payload.get("approved") is True or payload.get("verdict") == "APPROVE":
                break
            consecutive += 1
        if consecutive >= _SKILL_FAILURE_REJECT_THRESHOLD:
            from ...learnings import record_learning

            record_learning(
                conn,
                kind="skill_failure",
                subject=f"reviewer::{scope}",
                payload={"reason": reason, "consecutive": consecutive, "scope": scope},
                actor="review-gate",
            )
    except Exception:
        pass

def _review_sut_key(paths: RuntimePaths) -> str:
    """Best-effort SUT identifier for coverage_gap subjects (issue #287)."""
    try:
        from ...config import load_or_default

        sut = load_or_default(paths.repo_root).raw.get("sut") or {}
        return str(sut.get("root") or ".").strip() or "."
    except Exception:
        return "."

def _record_coverage_gap_from_review(
    conn: sqlite3.Connection, paths: RuntimePaths, *, scope: str, diff_text: str
) -> None:
    """Issue #287 (C2) — record a `coverage_gap` when the reviewed diff is
    missing coverage-floor markers for a ui/api scope.

    Evaluated with ``coverage_floor=False`` so the verdict is purely advisory:
    a missing floor surfaces as ``coverage_floor_missing`` regardless of the
    autonomy flag, and this producer records it without changing any gate
    result. Wholly best-effort: any failure is swallowed.
    """
    if scope not in {"ui", "api"} or not diff_text:
        return
    try:
        from ...coverage_review import evaluate_api_coverage, evaluate_ui_coverage
        from ...learnings import record_learning

        if scope == "ui":
            verdict = evaluate_ui_coverage(diff_text, coverage_floor=False)
        else:
            verdict = evaluate_api_coverage(diff_text, coverage_floor=False)
        if verdict.reason != "coverage_floor_missing":
            return
        sut_key = _review_sut_key(paths)
        record_learning(
            conn,
            kind="coverage_gap",
            subject=f"{sut_key}::coverage-floor-{scope}",
            payload={"missing": list(verdict.missing), "scope": scope},
            actor="coverage-review",
        )
    except Exception:
        pass

def run_review_gate(
    orchestrator: Orchestrator,
    paths: RuntimePaths,
    events: EventLog,
    *,
    diff_path: Optional[Path],
    scope: str,
    reviewer_output_path: Optional[Path] = None,
    apply_patch_path: Optional[Path] = None,
    work_item_id: Optional[str] = None,
) -> WorkflowResult:
    orchestrator.seed_phases()
    task_id = orchestrator.create_task(
        phase_id=CURRENT_PHASE_ID,
        kind="review",
        payload={"workflow": "review-gate", "scope": scope},
    )
    orchestrator.lease_task(task_id, owner="orchestrator", ttl_seconds=60)
    orchestrator.mark_running(task_id, owner="orchestrator")
    reviewer_lease_token: Optional[str] = None
    if work_item_id is not None:
        reviewer_lease_token = f"{task_id}:{ulid()}"
        if not _acquire_reviewer_lease(
            orchestrator.conn,
            work_item_id,
            token=reviewer_lease_token,
        ):
            return _review_gate_busy_result(
                orchestrator,
                paths,
                events,
                task_id=task_id,
                scope=scope,
                work_item_id=work_item_id,
            )

    started_at = now_iso()
    run_id_str = new_run_id()
    log_path = paths.subprocess_logs_dir / f"{run_id_str}.log"
    if diff_path is not None:
        diff_path = resolve_repo_path(paths.repo_root, str(diff_path), label="--diff", must_exist=True)
    if reviewer_output_path is not None:
        reviewer_output_path = resolve_repo_path(
            paths.repo_root, str(reviewer_output_path), label="--reviewer-output", must_exist=True
        )
    if apply_patch_path is not None:
        apply_patch_path = resolve_repo_path(
            paths.repo_root, str(apply_patch_path), label="--apply-patch", must_exist=True
        )

    # Issue #109 — what gets reviewed must equal what gets applied. Either
    # both files exist and their bytes are identical, or `--diff` is omitted
    # and the apply patch *is* the diff being reviewed. Mismatch becomes an
    # immediate REJECT so a benign diff cannot mask a dangerous patch.
    reviewed_patch_sha256: Optional[str] = None
    mismatch_finding: Optional[GateFinding] = None
    if apply_patch_path is not None and diff_path is not None:
        diff_bytes = diff_path.read_bytes()
        apply_bytes = apply_patch_path.read_bytes()
        if diff_bytes != apply_bytes:
            mismatch_finding = GateFinding(
                str(apply_patch_path.relative_to(paths.repo_root)),
                1,
                "reviewed diff does not match patch passed to --apply-patch "
                f"(diff sha256={hashlib.sha256(diff_bytes).hexdigest()}, "
                f"apply sha256={hashlib.sha256(apply_bytes).hexdigest()})",
            )
    if apply_patch_path is not None and diff_path is None:
        # Force review-what-we-apply: the apply patch is the diff source.
        diff_path = apply_patch_path

    diff_text = _read_diff(paths, diff_path)
    if apply_patch_path is not None:
        reviewed_patch_sha256 = hashlib.sha256(apply_patch_path.read_bytes()).hexdigest()
    if mismatch_finding is not None:
        gate = GateResult(
            verdict="REJECT",
            reason="diff_apply_patch_mismatch",
            findings=[mismatch_finding],
        )
    else:
        gate = static_review_gate(diff_text, scope=scope)
        if gate.approved and reviewer_output_path is not None:
            raw_reviewer_output = reviewer_output_path.read_text(encoding="utf-8")
            try:
                gate = parse_gate_output(raw_reviewer_output)
            except ValueError as exc:
                gate = GateResult(
                    verdict="REJECT",
                    reason="ambiguous_reviewer_output",
                    findings=[
                        GateFinding(
                            str(reviewer_output_path.relative_to(paths.repo_root)),
                            1,
                            str(exc),
                        )
                    ],
                    raw_output=raw_reviewer_output,
                )
    apply_result = None
    if apply_patch_path is not None:
        # Issue #242 — thread work_item context so the gate can branch +
        # autocommit when `git.enabled=true`. Title fetched best-effort.
        wi_title: Optional[str] = None
        if work_item_id is not None:
            try:
                from ...work_items import get_work_item

                wi_row = get_work_item(orchestrator.conn, work_item_id)
                if wi_row is not None:
                    wi_title = wi_row.get("title")
            except Exception:
                wi_title = None
        apply_result = merge_patch_if_approved(
            paths=paths,
            events=events,
            patch_path=apply_patch_path,
            gate=gate,
            reviewed_patch_sha256=reviewed_patch_sha256,
            work_item_id=work_item_id,
            work_item_title=wi_title,
        )
        # Issue #87 — an APPROVE gate alone does not resolve a patch.
        # Register an `apply` artifact only when `git apply` actually ran,
        # so final-gate can distinguish reviewed-but-not-applied patches
        # from real resolutions.
        if (
            apply_result is not None
            and apply_result.applied
            and work_item_id is not None
        ):
            _register_apply_artifact(
                paths=paths,
                events=events,
                work_item_id=work_item_id,
                patch_path=apply_patch_path,
            )
            # Issue #320 (Codex P1) — `git apply` actually landed the spec
            # files: now (and only now) is the coverage ledger truth. Ingest
            # the pending manifest written next to the patch by
            # `implement_tests_for_work_item`; rejected/unapplied patches
            # leave the ledger untouched, preserving regeneration.
            try:
                from ...coverage_ledger import ingest_pending_manifest

                manifest_path = apply_patch_path.with_suffix(".coverage.json")
                ingested = ingest_pending_manifest(orchestrator.conn, manifest_path)
                if ingested:
                    events.write(
                        "work_item.coverage_ledger_ingested",
                        payload={
                            "work_item_id": work_item_id,
                            "manifest_path": str(manifest_path),
                            "entries": ingested,
                        },
                    )
                    # Issue #332 — after a successful ingest the manifest is
                    # redundant (re-ingestion is idempotent and the ledger is
                    # now the truth). Delete so
                    # `agentic-os-runtime/patches/<id>/` stops accumulating one
                    # manifest per run. A failed ingest returns 0 and is
                    # preserved for retry, satisfying the issue invariant.
                    # Best-effort — a stale manifest on disk is harmless.
                    try:
                        manifest_path.unlink()
                    except OSError:
                        pass
            except Exception as exc:  # best-effort, never blocks apply success
                events.write(
                    "work_item.coverage_ledger_error",
                    payload={"work_item_id": work_item_id, "error": str(exc)},
                )
    # Issue #104 — bind the gate artifact to the exact patch reviewed
    # so resolution cannot match a sibling patch on the same work item.
    bound_patch_path = apply_patch_path or diff_path
    patch_metadata = None
    if bound_patch_path is not None:
        try:
            bound_rel = str(bound_patch_path.relative_to(paths.repo_root))
        except ValueError:
            bound_rel = str(bound_patch_path)
        bound_sha = reviewed_patch_sha256 or (
            hashlib.sha256(bound_patch_path.read_bytes()).hexdigest()
            if bound_patch_path.is_file()
            else None
        )
        patch_metadata = {"path": bound_rel, "sha256": bound_sha}
    gate_path = write_gate_result(
        paths, gate, name="review-gate", patch_metadata=patch_metadata
    )
    if work_item_id is not None:
        _attach_gate_to_work_item(
            paths=paths,
            events=events,
            work_item_id=work_item_id,
            scope=scope,
            gate=gate,
            gate_path=gate_path,
            apply_patch_path=apply_patch_path,
        )
    log_path.write_text(gate.to_text(), encoding="utf-8")

    # Issue #111 — manifest must be a sufficient audit artifact. Record
    # every input the gate decision actually used so reviewers can
    # reconstruct what was reviewed and what was applied, not just the
    # generic command name.
    def _rel(target: Optional[Path]) -> Optional[str]:
        return (
            str(target.relative_to(paths.repo_root))
            if target is not None
            else None
        )

    diff_rel = _rel(diff_path)
    apply_rel = _rel(apply_patch_path)
    reviewer_rel = _rel(reviewer_output_path)
    full_command: List[str] = ["agentic-os", "run", "review-gate", "--scope", scope]
    if diff_rel is not None:
        full_command.extend(["--diff", diff_rel])
    if apply_rel is not None:
        full_command.extend(["--apply-patch", apply_rel])
    if reviewer_rel is not None:
        full_command.extend(["--reviewer-output", reviewer_rel])
    if work_item_id is not None:
        full_command.extend(["--work-item", work_item_id])

    diff_sha256: Optional[str] = (
        hashlib.sha256(diff_path.read_bytes()).hexdigest()
        if diff_path is not None and diff_path.is_file()
        else None
    )
    applied_patch_sha256: Optional[str] = (
        reviewed_patch_sha256
        if apply_result is not None and apply_result.applied
        else None
    )
    audit_context: Dict[str, Any] = {
        "scope": scope,
        "work_item_id": work_item_id,
        "diff_path": diff_rel,
        "diff_sha256": diff_sha256,
        "apply_patch_path": apply_rel,
        "reviewed_patch_sha256": reviewed_patch_sha256,
        "applied_patch_sha256": applied_patch_sha256,
        "apply_attempted": apply_patch_path is not None,
        "apply_succeeded": bool(apply_result is not None and apply_result.applied),
        "reviewer_output_path": reviewer_rel,
        "gate_output_path": str(gate_path.relative_to(paths.repo_root)),
    }

    orchestrator.record_run(
        task_id=task_id,
        run_id=run_id_str,
        command=full_command,
        cwd=str(paths.repo_root),
        env_hash=env_hash(),
        log_path=str(log_path.relative_to(paths.repo_root)),
        started_at=started_at,
    )
    finished_at = now_iso()
    exit_code = 0 if gate.approved else 1
    manifest_path = write_manifest(
        paths=paths,
        run_id_str=run_id_str,
        task_id=task_id,
        kind="review-gate",
        command=full_command,
        cwd=str(paths.repo_root),
        started_at=started_at,
        finished_at=finished_at,
        exit_code=exit_code,
        failure_kind=None if gate.approved else "product",
        extra={
            "gate": gate.to_json(),
            "gate_output_path": str(gate_path.relative_to(paths.repo_root)),
            "audit_context": audit_context,
        },
    )
    orchestrator.finish_run(
        run_id=run_id_str,
        exit_code=exit_code,
        duration_ms=0,
        failure_kind=None if gate.approved else "product",
        unmapped_exit=False,
        evidence_path=str((paths.evidence_dir / run_id_str).relative_to(paths.repo_root)),
        manifest_path=str(manifest_path.relative_to(paths.repo_root)),
        finished_at=finished_at,
    )
    orchestrator.finish_task(
        task_id,
        status="succeeded" if gate.approved else "failed",
        exit_code=exit_code,
        error_class=None if gate.approved else gate.reason,
    )
    events.write(
        "gate.review_finished",
        task_id=task_id,
        run_id=run_id_str,
        severity="info" if gate.approved else "warning",
        # Issue #287 — carry `scope` in the payload so the skill_failure
        # detector below can cluster consecutive REJECTs by reviewer family.
        payload={**gate.to_json(), "scope": scope},
    )
    # Issue #287 — skill_failure producer. A persistently-rejecting reviewer
    # for one scope is a learning: the implementer should pre-empt the cluster
    # next time. Best-effort and advisory; a record failure must never change
    # the gate result or the WorkflowResult.
    if not gate.approved:
        _record_skill_failure_on_persistent_reject(
            orchestrator.conn, scope=scope, reason=gate.reason
        )
    # Issue #287 (C2) — coverage_gap producer. For ui/api scopes, score the
    # reviewed diff against the coverage-floor marker contract; when the floor
    # is missing, record an advisory coverage_gap. RECORD-ONLY: this never
    # touches the gate verdict or the WorkflowResult. Best-effort.
    _record_coverage_gap_from_review(
        orchestrator.conn, paths, scope=scope, diff_text=diff_text
    )
    if work_item_id is not None and reviewer_lease_token is not None:
        _release_reviewer_lease(
            orchestrator.conn,
            work_item_id,
            token=reviewer_lease_token,
        )
    return WorkflowResult(
        ok=gate.approved,
        exit_code=exit_code,
        failure_kind=None if gate.approved else "product",
        task_id=task_id,
        run_id=run_id_str,
        manifest_path=str(manifest_path.relative_to(paths.repo_root)),
        reports_path=None,
        bugs_opened=[],
    )
