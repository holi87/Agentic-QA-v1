"""Run-artifact / patch-artifact attachment helpers (issue #292)."""
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



def _display_command(repo_root: Path, command_path: Path) -> str:
    try:
        rel = command_path.relative_to(repo_root.resolve())
    except ValueError:
        return str(command_path)
    return "./" + str(rel)

def _register_apply_artifact(
    *,
    paths: RuntimePaths,
    events: EventLog,
    work_item_id: str,
    patch_path: Path,
) -> None:
    """Record that a patch was applied to the working tree (issue #87).

    Final gate uses presence of an `apply` artifact (in addition to an
    APPROVE gate verdict) to consider a patch resolved. Failure here is
    logged but never raised — gate verdict already succeeded.
    """
    from ...work_items import (
        get_work_item,
        register_work_item_artifact,
    )

    try:
        conn = _db_connect(paths.db)
    except Exception as exc:  # pragma: no cover — db should exist
        events.write(
            "gate.apply_register_failed",
            severity="warning",
            payload={"work_item_id": work_item_id, "error": str(exc)},
        )
        return
    try:
        if get_work_item(conn, work_item_id) is None:
            events.write(
                "gate.apply_register_skipped",
                severity="warning",
                payload={"work_item_id": work_item_id, "reason": "unknown_work_item"},
            )
            return
        rel = str(patch_path.resolve().relative_to(paths.repo_root.resolve()))
        try:
            register_work_item_artifact(
                conn,
                paths,
                events,
                work_item_id=work_item_id,
                kind="apply",
                path=rel,
            )
        except UsageError as exc:
            events.write(
                "gate.apply_register_failed",
                severity="warning",
                payload={"work_item_id": work_item_id, "error": str(exc)},
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _attach_gate_to_work_item(
    *,
    paths: RuntimePaths,
    events: EventLog,
    work_item_id: str,
    scope: str,
    gate: Any,
    gate_path: Path,
    apply_patch_path: Optional[Path],
) -> None:
    """Persist a gate artifact and update task status for dashboard display.

    Connection is opened on a per-call basis to avoid touching the runtime
    DB owned by the orchestrator. Failures here must not mask the gate's own
    verdict — they are logged but never raised.
    """
    from ...work_items import (
        get_work_item,
        register_work_item_artifact,
        update_work_item_status,
    )

    try:
        conn = _db_connect(paths.db)
    except Exception as exc:  # pragma: no cover — db should always exist here
        events.write(
            "gate.work_item_attach_failed",
            severity="warning",
            payload={"work_item_id": work_item_id, "error": str(exc)},
        )
        return
    try:
        if get_work_item(conn, work_item_id) is None:
            events.write(
                "gate.work_item_attach_skipped",
                severity="warning",
                payload={"work_item_id": work_item_id, "reason": "unknown_work_item"},
            )
            return
        rel = str(gate_path.resolve().relative_to(paths.repo_root.resolve()))
        try:
            register_work_item_artifact(
                conn,
                paths,
                events,
                work_item_id=work_item_id,
                kind="gate",
                path=rel,
            )
        except UsageError as exc:
            events.write(
                "gate.work_item_attach_failed",
                severity="warning",
                payload={"work_item_id": work_item_id, "error": str(exc)},
            )
            return
        next_status = "reviewing" if gate.approved else "blocked"
        try:
            update_work_item_status(
                conn, events, work_item_id=work_item_id, status=next_status
            )
        except UsageError as exc:
            events.write(
                "gate.work_item_status_update_failed",
                severity="warning",
                payload={
                    "work_item_id": work_item_id,
                    "status": next_status,
                    "error": str(exc),
                },
            )
        events.write(
            "gate.attached_to_work_item",
            payload={
                "work_item_id": work_item_id,
                "scope": scope,
                "verdict": gate.verdict,
                "reason": gate.reason,
                "gate_path": rel,
                "patch_path": (
                    str(apply_patch_path.resolve().relative_to(paths.repo_root.resolve()))
                    if apply_patch_path is not None
                    else None
                ),
            },
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _attach_run_artifacts_to_work_item(
    *,
    paths: RuntimePaths,
    events: EventLog,
    work_item_id: str,
    kind: str,
    result: "WorkflowResult",
    evidence_subdir: str,
    report_ok: bool,
    extra_gate_path: Optional[Path] = None,
) -> None:
    """Register run, manifest, report and evidence artifacts on a work item.

    Mirrors the resilience of `_attach_gate_to_work_item` — never lets
    artifact bookkeeping mask the underlying workflow outcome. Status
    transitions are intentionally narrow: only flip `running`/`done`/`failed`
    when the workflow type matches and the work item is not already in a
    later state owned by a different action.
    """
    from ...work_items import (
        get_work_item,
        register_work_item_artifact,
        update_work_item_status,
    )

    try:
        conn = _db_connect(paths.db)
    except Exception as exc:  # pragma: no cover - db should exist here
        events.write(
            "workflow.work_item_attach_failed",
            severity="warning",
            payload={"work_item_id": work_item_id, "error": str(exc)},
        )
        return

    try:
        if get_work_item(conn, work_item_id) is None:
            events.write(
                "workflow.work_item_attach_skipped",
                severity="warning",
                payload={"work_item_id": work_item_id, "reason": "unknown_work_item"},
            )
            return

        manifest_rel = result.manifest_path
        evidence_rel = str(
            (paths.evidence_dir / evidence_subdir).resolve().relative_to(paths.repo_root.resolve())
        )

        artifact_specs: List[tuple[str, str]] = [
            ("run", manifest_rel),
            ("evidence", evidence_rel),
        ]
        if report_ok and result.reports_path:
            artifact_specs.append(("report", result.reports_path))
        if extra_gate_path is not None:
            try:
                rel_gate = str(
                    extra_gate_path.resolve().relative_to(paths.repo_root.resolve())
                )
                artifact_specs.append(("gate", rel_gate))
            except ValueError:
                pass

        for art_kind, rel in artifact_specs:
            try:
                register_work_item_artifact(
                    conn,
                    paths,
                    events,
                    work_item_id=work_item_id,
                    kind=art_kind,
                    path=rel,
                )
            except UsageError as exc:
                events.write(
                    "workflow.work_item_attach_failed",
                    severity="warning",
                    payload={
                        "work_item_id": work_item_id,
                        "artifact_kind": art_kind,
                        "error": str(exc),
                    },
                )

        if kind == "run-tests":
            if result.ok:
                next_status = "done"
            elif result.failure_kind == "infra":
                next_status = "blocked"
            elif result.failure_kind == "user_abort":
                next_status = None
            else:
                next_status = "failed"
        elif kind == "final-gate":
            next_status = "done" if result.ok else "blocked"
        else:
            next_status = None

        if next_status is not None:
            try:
                update_work_item_status(
                    conn,
                    events,
                    work_item_id=work_item_id,
                    status=next_status,
                )
            except UsageError as exc:
                events.write(
                    "workflow.work_item_status_update_failed",
                    severity="warning",
                    payload={
                        "work_item_id": work_item_id,
                        "status": next_status,
                        "error": str(exc),
                    },
                )

        events.write(
            "workflow.attached_to_work_item",
            payload={
                "work_item_id": work_item_id,
                "kind": kind,
                "exit_code": result.exit_code,
                "failure_kind": result.failure_kind,
                "ok": result.ok,
                "run_id": result.run_id,
                "manifest_path": manifest_rel,
                "reports_path": result.reports_path,
                "evidence_path": evidence_rel,
            },
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _read_diff(paths: RuntimePaths, diff_path: Optional[Path]) -> str:
    if diff_path is None:
        log_path = paths.tmp_dir / "review-gate-git-diff.log"
        result = run_command(
            ["git", "diff", "--"],
            cwd=paths.repo_root,
            log_path=log_path,
            timeout_seconds=60,
        )
        if result.exit_code != 0:
            raise UsageError(f"git diff failed; see {log_path}")
        raw = log_path.read_text(encoding="utf-8")
        lines = []
        for line in raw.splitlines():
            if line.startswith("[stdout] "):
                lines.append(line[len("[stdout] "):])
        return "\n".join(lines) + ("\n" if lines else "")
    return diff_path.read_text(encoding="utf-8")
