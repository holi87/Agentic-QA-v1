"""run_tests pipeline stage (issue #292)."""
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
from .attachments import _attach_run_artifacts_to_work_item, _display_command
from .dry_run import _augment_manifest, _clean_report_source_artifacts, env_hash, write_manifest
from .finalize import _zero_test_report_status, finalize_reports
from .idempotency import _find_run_by_idempotency_key, _run_tests_idempotency_key, _workflow_result_from_run_row
from .triage import triage_reports


def _declared_secret_env_names(sut_cfg: Dict[str, Any]) -> List[str]:
    """Env-var NAMES the SUT config declares as secret-bearing (issue #385).

    ``sut.db`` / ``sut.credentials`` with ``ref_type: env`` name an env var
    holding a DSN / token whose name may carry no redaction keyword. Their
    values must still be scrubbed from the test-runner log, so collect the
    declared names and thread them into the subprocess redaction.
    """
    names: List[str] = []
    for block_key in ("db", "credentials"):
        block = sut_cfg.get(block_key)
        if isinstance(block, dict) and block.get("ref_type") == "env":
            value = block.get("value")
            if isinstance(value, str) and value:
                names.append(value)
    return names



def run_tests(
    orchestrator: Orchestrator,
    paths: RuntimePaths,
    events: EventLog,
    *,
    tag: Optional[str] = None,
    work_item_id: Optional[str] = None,
) -> WorkflowResult:
    from ...config import load_or_default
    from ...runner import run_and_record

    cfg = load_or_default(paths.repo_root)
    runner_path = resolve_repo_path(
        paths.repo_root,
        str(cfg.raw["sut"]["test_runner"]),
        label="sut.test_runner",
        must_exist=True,
    )
    command = [_display_command(paths.repo_root, runner_path)]
    if tag:
        command.append(tag)
    idempotency_key = _run_tests_idempotency_key(
        orchestrator.conn,
        paths,
        work_item_id=work_item_id,
        tag=tag,
        command=command,
    )
    existing_run = _find_run_by_idempotency_key(orchestrator.conn, idempotency_key)
    if existing_run is not None:
        events.write(
            "run_tests.idempotent_replay",
            task_id=str(existing_run["task_id"]),
            run_id=str(existing_run["id"]),
            severity="info" if existing_run["finished_at"] is not None else "warning",
            payload={
                "work_item_id": work_item_id,
                "idempotency_key": idempotency_key,
                "finished": existing_run["finished_at"] is not None,
            },
        )
        return _workflow_result_from_run_row(paths, existing_run)

    orchestrator.seed_phases()
    task_id = orchestrator.create_task(
        phase_id=CURRENT_PHASE_ID,
        kind="run",
        payload={"workflow": "run-tests", "resume_allowed": True, "tag": tag},
    )
    orchestrator.lease_task(task_id, owner="orchestrator", ttl_seconds=60)
    orchestrator.mark_running(task_id, owner="orchestrator")

    # Issue #108 — bring up / healthcheck the SUT before invoking the
    # runner. Local mode with `autostart=true` runs `docker compose up`
    # and waits for the configured healthcheck. Online mode performs
    # an HTTP healthcheck against the configured URL. Lifecycle
    # failures abort the run with infra exit 2 instead of pretending
    # to be a product failure.
    sut_lifecycle_logs: List[Dict[str, Any]] = []
    sut_section_cfg = cfg.raw.get("sut") or {}
    sut_mode = (sut_section_cfg.get("mode") or "local").lower()
    sut_lifecycle_ok = True
    sut_lifecycle_error: Optional[str] = None
    compose_file_path = sut_section_cfg.get("compose_file")
    # Issue #187 — previously this branch was additionally gated on
    # `compose_exists`, so when `sut.mode=local` + `autostart=true` and
    # the configured compose file did not exist on disk (or was null),
    # `run-tests` silently skipped lifecycle and invoked the test runner
    # against a SUT that was never started. `doctor --sut` and `run
    # sut-start` both treat the missing compose file as
    # `infra_missing_compose_file`; `run-tests` must agree. We now
    # always delegate to `run_sut_start` when local autostart is on:
    # it skips cleanly when `compose_file` is null and returns infra
    # exit 2 when the configured path is missing on disk.
    if (
        sut_mode == "local"
        and bool(sut_section_cfg.get("autostart"))
    ):
        try:
            from ...sut_lifecycle import run_sut_start, run_sut_healthcheck

            start_res = run_sut_start(
                paths,
                events,
                compose_file=sut_section_cfg.get("compose_file"),
                compose_project_name=sut_section_cfg.get("compose_project_name"),
            )
            sut_lifecycle_logs.append(
                {
                    "step": "sut_start",
                    "ok": start_res.ok,
                    "failure_kind": start_res.failure_kind,
                    "detail": start_res.detail,
                }
            )
            if not start_res.ok:
                sut_lifecycle_ok = False
                sut_lifecycle_error = (
                    start_res.failure_kind or "sut_start_failed"
                )
            elif (start_res.detail or {}).get("skipped"):
                # `compose_file: null` — operator opted out of a
                # compose-managed SUT. Preserve the pre-#187 behaviour
                # of letting the runner take over without an extra
                # healthcheck.
                pass
            else:
                hc_res = run_sut_healthcheck(
                    paths,
                    events,
                    healthcheck_cfg=sut_section_cfg.get("healthcheck") or {},
                )
                sut_lifecycle_logs.append({"step": "sut_healthcheck", "ok": hc_res.ok})
                if not hc_res.ok:
                    sut_lifecycle_ok = False
                    sut_lifecycle_error = "sut_healthcheck_failed"
        except Exception as exc:
            sut_lifecycle_ok = False
            sut_lifecycle_error = f"sut_lifecycle_exception: {exc}"
            sut_lifecycle_logs.append({"step": "sut_lifecycle", "error": str(exc)})
    elif sut_mode == "online" and bool(sut_section_cfg.get("autostart")):
        # Light HTTP healthcheck — only when the operator explicitly
        # opted into autostart for online mode. Otherwise the runner
        # is responsible for checking SUT readiness itself.
        url = (sut_section_cfg.get("web") or {}).get("url") or (
            sut_section_cfg.get("api") or {}
        ).get("url")
        if isinstance(url, str) and url.strip():
            try:
                import urllib.error
                import urllib.request

                req = urllib.request.Request(url, method="GET")
                try:
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        sut_lifecycle_logs.append(
                            {"step": "sut_online_check", "status": resp.status}
                        )
                except urllib.error.HTTPError as http_err:
                    sut_lifecycle_logs.append(
                        {"step": "sut_online_check", "status": http_err.code}
                    )
                    if http_err.code >= 500:
                        sut_lifecycle_ok = False
                        sut_lifecycle_error = f"sut_online_status_{http_err.code}"
            except Exception as exc:
                sut_lifecycle_logs.append(
                    {"step": "sut_online_check", "error": str(exc)}
                )
                sut_lifecycle_ok = False
                sut_lifecycle_error = f"sut_online_unreachable: {exc}"
    if not sut_lifecycle_ok:
        # Codex review on #130 (P1): the task was already marked
        # running. Finalize it through the orchestrator so queue
        # progress and status/recovery views stay consistent on the
        # lifecycle-infra-failure path. We also record a manifest so
        # the failure is auditable like any other run.
        sut_run_id = new_run_id()
        sut_started = now_iso()
        sut_finished = sut_started
        sut_log_path = paths.subprocess_logs_dir / f"{sut_run_id}.log"
        sut_log_path.parent.mkdir(parents=True, exist_ok=True)
        sut_log_path.write_text(
            f"sut lifecycle failed: {sut_lifecycle_error}\n", encoding="utf-8"
        )
        try:
            orchestrator.record_run(
                task_id=task_id,
                run_id=sut_run_id,
                idempotency_key=idempotency_key,
                command=["agentic-os", "run", "run-tests"],
                cwd=str(paths.repo_root),
                env_hash=env_hash(),
                log_path=str(sut_log_path.relative_to(paths.repo_root)),
                started_at=sut_started,
            )
            sut_manifest = write_manifest(
                paths=paths,
                run_id_str=sut_run_id,
                task_id=task_id,
                kind="run-tests",
                command=["agentic-os", "run", "run-tests"],
                cwd=str(paths.repo_root),
                started_at=sut_started,
                finished_at=sut_finished,
                exit_code=2,
                failure_kind="infra",
                extra={
                    "sut_lifecycle": {
                        "ok": False,
                        "error": sut_lifecycle_error,
                        "log": sut_lifecycle_logs,
                    },
                },
            )
            orchestrator.finish_run(
                run_id=sut_run_id,
                exit_code=2,
                duration_ms=0,
                failure_kind="infra",
                unmapped_exit=False,
                evidence_path=str(
                    (paths.evidence_dir / sut_run_id).relative_to(paths.repo_root)
                ),
                manifest_path=str(sut_manifest.relative_to(paths.repo_root)),
                finished_at=sut_finished,
            )
        except Exception as record_exc:
            events.write(
                "run_tests.sut_lifecycle_record_failed",
                severity="warning",
                payload={"error": str(record_exc)},
            )
            sut_manifest = None
        finally:
            try:
                orchestrator.finish_task(
                    task_id,
                    status="failed",
                    exit_code=2,
                    error_class=sut_lifecycle_error,
                )
            except Exception as finish_exc:
                events.write(
                    "run_tests.sut_lifecycle_finish_failed",
                    severity="error",
                    payload={"error": str(finish_exc)},
                )
        events.write(
            "run_tests.sut_lifecycle_failed",
            task_id=task_id,
            run_id=sut_run_id,
            severity="error",
            payload={"error": sut_lifecycle_error, "log": sut_lifecycle_logs},
        )
        return WorkflowResult(
            ok=False,
            exit_code=2,
            failure_kind="infra",
            task_id=task_id,
            run_id=sut_run_id,
            manifest_path=(
                str(sut_manifest.relative_to(paths.repo_root))
                if isinstance(sut_manifest, Path) and sut_manifest.is_file()
                else ""
            ),
            reports_path=None,
            bugs_opened=[],
        )

    # Issue #92 — inject API_BASE_URL / UI_BASE_URL from config into
    # the test runner env so generated Playwright specs can launch
    # without the operator manually exporting them. Existing operator
    # env vars take precedence (explicit > task override > config).
    sut_cfg = cfg.raw.get("sut") or {}
    # Issue #291 — the test_runner is SUT-supplied; never hand it the
    # operator's model credentials. Strip them before they reach the child.
    env_override: Dict[str, str] = scrub_provider_credentials(os.environ)
    api_block = sut_cfg.get("api") or {}
    web_block = sut_cfg.get("web") or {}
    if "API_BASE_URL" not in env_override and isinstance(api_block.get("url"), str):
        env_override["API_BASE_URL"] = api_block["url"]
    if "UI_BASE_URL" not in env_override and isinstance(web_block.get("url"), str):
        env_override["UI_BASE_URL"] = web_block["url"]
    _clean_report_source_artifacts(paths, events)
    record = run_and_record(
        orchestrator=orchestrator,
        paths=paths,
        events=events,
        task_id=task_id,
        kind="run-tests",
        command=command,
        timeout_seconds=int(cfg.raw["runtime"]["timeouts"]["test_seconds"]),
        shutdown_grace_seconds=int(cfg.raw["runtime"]["shutdown_grace_seconds"]),
        env=env_override,
        idempotency_key=idempotency_key,
        include_provider_credentials=False,
        secret_env_names=_declared_secret_env_names(sut_cfg),
    )
    report_ok, report_errors = finalize_reports(paths, events)
    result = record.result
    final_exit = result.exit_code
    failure_kind = result.failure_kind
    if final_exit == 1 and not report_ok:
        final_exit = 2
        failure_kind = "infra"
        events.write(
            "reports.required_missing",
            task_id=task_id,
            run_id=record.run_id,
            severity="error",
            payload={"errors": report_errors},
        )
    elif final_exit != 0 and report_ok:
        events.write(
            "reports.finalized_before_nonzero",
            task_id=task_id,
            run_id=record.run_id,
            severity="info",
            payload={"exit_code": final_exit, "reports_path": "reports"},
        )

    # Issue #100 — a runner exit of 0 with zero collected tests is not
    # a green run, it's a silent infra failure (missing JUnit, broken
    # discovery, runner pointing at the wrong dir). Promote to infra
    # exit 2 unless the report explicitly marks itself as a
    # discovery/dry-run with `discovery_only: true` (or `dry_run: true`).
    if final_exit == 0 and report_ok:
        zero, discovery_only = _zero_test_report_status(paths)
        if zero and not discovery_only:
            final_exit = 2
            failure_kind = "infra"
            events.write(
                "reports.zero_tests_collected",
                task_id=task_id,
                run_id=record.run_id,
                severity="error",
                payload={
                    "reason": "runner exited 0 but reports/last-run.json total=0",
                    "next_action": (
                        "verify the runner discovers tests; if intentional, "
                        "set `discovery_only: true` in reports/last-run.json"
                    ),
                },
            )

    triage = triage_reports(
        paths,
        events,
        run_id_str=record.run_id,
        auto_file_bugs=bool(
            report_ok
            and final_exit == 1
            and cfg.raw.get("gates", {}).get("exact_spec_failure_opens_bug", True)
        ),
    ) if report_ok else {
        "available": False,
        "reason": "reports_not_finalized",
        "bugs_opened": [],
    }

    _augment_manifest(
        record.manifest_path,
        {
            "reports": {
                "path": "reports",
                "finalized": report_ok,
                "errors": report_errors,
            },
            "triage": triage,
            "effective_exit_code": final_exit,
            "effective_failure_kind": failure_kind,
        },
    )

    if final_exit == 0:
        task_status = "succeeded"
    elif final_exit == 130:
        task_status = "cancelled"
        failure_kind = "user_abort"
    else:
        task_status = "failed"
    orchestrator.finish_task(
        task_id,
        status=task_status,
        exit_code=final_exit,
        error_class=failure_kind,
    )

    result_obj = WorkflowResult(
        ok=final_exit == 0,
        exit_code=final_exit,
        failure_kind=failure_kind,
        task_id=task_id,
        run_id=record.run_id,
        manifest_path=str(record.manifest_path.relative_to(paths.repo_root)),
        reports_path="reports" if report_ok else None,
        bugs_opened=list(triage.get("bugs_opened") or []),
    )
    if work_item_id is not None:
        _attach_run_artifacts_to_work_item(
            paths=paths,
            events=events,
            work_item_id=work_item_id,
            kind="run-tests",
            result=result_obj,
            evidence_subdir=record.run_id,
            report_ok=report_ok,
        )
    return result_obj
