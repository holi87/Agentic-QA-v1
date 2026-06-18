"""Diagnostic commands: doctor, status, logs, support-bundle (issue #292)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..errors import AgenticOSError, ConfigError, InfraError, ProductFailure, UsageError, UserAbort
from ..orchestrator import (
    CURRENT_PHASE_ID,
    fetch_active_leases,
    fetch_bug_summary,
    fetch_last_run,
    fetch_phase_rows,
    fetch_task_summary,
    list_open_blockers,
    open_runtime,
)
from ..paths import detect_repo_root, runtime_paths_from_config
from ..storage.db import SCHEMA_NAME, SCHEMA_VERSION, transaction
from ..time_utils import now_iso
from ..security import require_safe_argv, resolve_repo_path
from ..analysis import analyze_work_item
from ..patch_builder import implement_tests_for_work_item
from ..test_planning import (
    plan_work_item,
    read_plan_candidates,
    approve_all_runnable_candidates,
    update_plan_candidate_decision,
)
from ..work_items import (
    annotate_spec_status,
    create_work_item_from_file,
    get_work_item_detail,
    link_work_items,
    list_work_items,
    prune_orphan_work_items,
)
from ..inbox import ingest_inbox, list_inbox_files, synthesize_inbox_task
from ..workflows import run_dry_run, run_final_gate, run_recovery, run_review_gate, run_tests
from .cmd_lifecycle import _DASHBOARD_LOGFILE_NAME


def _build_autonomy_doctor(
    repo_root: Path,
    paths: Any,
    cfg_raw: Dict[str, Any],
    cfg_error: Optional[str],
    *,
    config_override: Optional[Path] = None,
    model_smoke_timeout_seconds: int = 20,
) -> Dict[str, Any]:
    """Issue #266 — autonomy readiness: flags, budget, providers, git.

    Carries its own `exit_code` so `doctor --autonomy` can map to the CI
    contract: 0 ready / 2 config bad / 3 provider unavailable /
    4 budget misconfigured. Provider/budget checks only run when config
    loaded cleanly.
    """
    autonomy_cfg = (cfg_raw.get("autonomy") or {}) if isinstance(cfg_raw, dict) else {}
    flags = {
        flag: bool(autonomy_cfg.get(flag))
        for flag in (
            "coverage_floor",
            "coverage_architect",
            "triage_batch",
            "exploratory_baseline",
        )
    }
    section: Dict[str, Any] = {"flags": flags}

    if cfg_error:
        section["config_ok"] = False
        section["exit_code"] = 2
        section["ok"] = False
        return section
    section["config_ok"] = True

    # Budget config already passes the schema validator when config loads,
    # so a clean load means budgets are well-formed. Surface the limits.
    budgets = (cfg_raw.get("budgets") or {}) if isinstance(cfg_raw, dict) else {}
    section["budget"] = {
        "configured": bool(budgets),
        "fail_mode": budgets.get("fail_mode", "abort"),
        "session_max_tokens": (budgets.get("session") or {}).get("max_tokens"),
        "session_max_usd": (budgets.get("session") or {}).get("max_usd"),
        "ok": True,
    }

    # Provider smoke per role + fallbacks (reuses doctor_check_models).
    from ..sut_lifecycle import doctor_check_models

    models = doctor_check_models(
        cfg_raw.get("models") or {},
        smoke_timeout_seconds=model_smoke_timeout_seconds,
    )
    provider_issues = models.get("issues") if isinstance(models, dict) else None
    section["providers"] = {
        "ok": not provider_issues,
        "issues": provider_issues or [],
    }

    git_cfg = (cfg_raw.get("git") or {}) if isinstance(cfg_raw, dict) else {}
    section["git"] = {
        "enabled": bool(git_cfg.get("enabled")),
        "binary_on_path": shutil.which("git") is not None,
        "ok": (not git_cfg.get("enabled")) or shutil.which("git") is not None,
    }

    # Issue #271 — validate cron strings + flag stuck schedules. Best-effort;
    # a runtime/db failure here must not block the readiness verdict.
    try:
        from ..scheduler import audit_schedules, list_schedules
        from ..storage.db import init_db

        conn = init_db(paths.db)
        try:
            schedules = list_schedules(conn)
        finally:
            conn.close()
        section["schedules"] = audit_schedules(schedules)
    except Exception as exc:
        section["schedules"] = {
            "count": 0,
            "issues": [],
            "warnings": [],
            "ok": True,
            "error": str(exc),
        }

    if not section["providers"]["ok"]:
        section["exit_code"] = 3
    elif not section["budget"]["ok"]:
        section["exit_code"] = 4
    else:
        section["exit_code"] = 0
    # Invalid cron strings flip the autonomy verdict to not-ready (exit 2 —
    # configuration is broken) without overriding a provider/budget failure.
    schedules_section = section.get("schedules") or {}
    if schedules_section.get("issues") and section["exit_code"] == 0:
        section["exit_code"] = 2
    section["ok"] = section["exit_code"] == 0
    return section


def build_doctor_payload(
    repo_root: Path,
    *,
    config_override: Optional[Path] = None,
    include_sut: bool = False,
    include_models: bool = False,
    include_docker: bool = False,
    include_autonomy: bool = False,
    model_smoke_timeout_seconds: int = 20,
) -> Dict[str, Any]:
    """Construct the doctor JSON payload without printing or exiting.

    Extracted from `cmd_doctor` so the support-bundle builder can embed an
    up-to-date doctor snapshot without spawning a subprocess. The `ok` /
    `blocking_reasons` fields keep their meaning from #96.
    """
    config_canonical = repo_root / "config" / "agentic-os.yml"
    config_legacy = repo_root / ".qualitycat" / "agentic-os.yml"
    paths = runtime_paths_from_config(repo_root, override=config_override)
    from ..paths import DEFAULT_RUNTIME_ROOT, LEGACY_RUNTIME_ROOT

    # Codex review on #142: the repo supports `runtime.root: .agentic-os`
    # as an explicit operator choice via `runtime_paths_from_config`.
    # When the configured runtime IS the legacy path, advising
    # `migrate-runtime` would push operators away from their own
    # configured layout. Suppress the warning in that case.
    configured_root = paths.runtime_root.resolve()
    legacy_runtime = (repo_root / LEGACY_RUNTIME_ROOT).resolve()
    visible_runtime = (repo_root / DEFAULT_RUNTIME_ROOT).resolve()
    legacy_is_configured = configured_root == legacy_runtime

    runtime_warnings: List[str] = []
    if not legacy_is_configured:
        if visible_runtime.exists() and legacy_runtime.exists():
            runtime_warnings.append(
                f"both {DEFAULT_RUNTIME_ROOT}/ and {LEGACY_RUNTIME_ROOT}/ exist; "
                f"run `agentic-os migrate-runtime` to consolidate (issue #142)"
            )
        elif legacy_runtime.exists() and not visible_runtime.exists():
            runtime_warnings.append(
                f"only the legacy {LEGACY_RUNTIME_ROOT}/ runtime is present; "
                f"run `agentic-os migrate-runtime` to move it to {DEFAULT_RUNTIME_ROOT}/"
            )
    payload: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "repo_root": str(repo_root),
        "runtime_root": str(paths.runtime_root.relative_to(repo_root)),
        "runtime_root_exists": paths.runtime_root.exists(),
        "config_exists": config_canonical.exists() or config_legacy.exists(),
        "config": {
            "canonical_path": "config/agentic-os.yml",
            "canonical_exists": config_canonical.exists(),
            "legacy_path": ".qualitycat/agentic-os.yml",
            "legacy_exists": config_legacy.exists(),
            "source": None,
            "compatibility_note": (
                "legacy config is fallback only; canonical path is config/agentic-os.yml"
                if config_legacy.exists() and not config_canonical.exists()
                else None
            ),
        },
        "runtime": {
            "canonical_path": DEFAULT_RUNTIME_ROOT,
            "canonical_exists": visible_runtime.exists(),
            "legacy_path": LEGACY_RUNTIME_ROOT,
            "legacy_exists": legacy_runtime.exists(),
            "configured_root": str(paths.runtime_root.relative_to(repo_root)),
            "legacy_is_configured": legacy_is_configured,
            "warnings": runtime_warnings,
        },
    }
    from ..sut_lifecycle import doctor_check_docker, doctor_check_models, doctor_check_sut

    cfg_raw: Dict[str, Any] = {}
    cfg_error: Optional[str] = None
    try:
        from ..config import load_or_default

        cfg = load_or_default(repo_root, override=config_override)
        cfg_raw = cfg.raw
        try:
            payload["config"]["source"] = str(cfg.source.relative_to(repo_root))
        except ValueError:
            # Override config can live outside repo_root.
            payload["config"]["source"] = str(cfg.source)
        payload["config"]["override_active"] = config_override is not None
    except Exception as exc:
        cfg_error = str(exc)
        payload["config"]["error"] = cfg_error

    if include_docker:
        payload["docker"] = doctor_check_docker()
    if include_sut:
        if cfg_error:
            payload["sut"] = {"error": f"cannot load config: {cfg_error}"}
        else:
            payload["sut"] = doctor_check_sut(paths, cfg_raw.get("sut") or {})
    if include_models:
        if cfg_error:
            payload["models"] = {"error": f"cannot load config: {cfg_error}"}
        else:
            payload["models"] = doctor_check_models(
                cfg_raw.get("models") or {},
                smoke_timeout_seconds=model_smoke_timeout_seconds,
            )

    if include_autonomy:
        payload["autonomy"] = _build_autonomy_doctor(
            repo_root,
            paths,
            cfg_raw,
            cfg_error,
            config_override=config_override,
            model_smoke_timeout_seconds=model_smoke_timeout_seconds,
        )

    # Issue #96 — doctor must be a strict gate. Any requested check
    # with `issues` (or a config load error) makes doctor exit non-zero.
    # Warnings remain exit 0. A top-level `ok` boolean is added to the
    # payload so consumers can read the verdict without grepping.
    blocking_reasons: List[str] = []
    if cfg_error:
        blocking_reasons.append(f"config_error: {cfg_error}")
    for section in ("sut", "models", "docker"):
        block = payload.get(section)
        if not isinstance(block, dict):
            continue
        if block.get("error"):
            blocking_reasons.append(f"{section}.error: {block['error']}")
        issues = block.get("issues")
        if isinstance(issues, list) and issues:
            for issue in issues:
                blocking_reasons.append(f"{section}.issue: {issue}")
        if section == "docker":
            # `docker_check` returns ok=False even when the operator
            # never asked for docker (e.g. online SUT). Only treat it
            # as blocking when the operator explicitly requested it.
            if block.get("ok") is False:
                blocking_reasons.append("docker.ok=false")
    ok = not blocking_reasons
    payload["ok"] = ok
    if blocking_reasons:
        payload["blocking_reasons"] = blocking_reasons
    return payload


def _cmd_doctor_repair(
    repo_root: Path,
    *,
    apply: bool,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    """Issue #274 — run the self-healing repair scan.

    SAFETY: ``apply=False`` is a pure dry-run (no mutation), safe in non-tty /
    autonomous contexts. ``apply=True`` is reached only via ``--repair --yes``.
    """
    from .. import repair as _repair

    conn, paths, events, _ = open_runtime(repo_root)
    try:
        result = _repair.repair(conn, paths, events, apply=apply)
    finally:
        conn.close()
    if json_output:
        sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    else:
        mode = "APPLIED" if apply else "DRY RUN (pass --yes to apply)"
        sys.stdout.write(f"doctor --repair [{mode}]\n")
        sys.stdout.write(f"  findings: {result['total']} (safe={result['safe_count']} hard={result['hard_count']})\n")
        for cls, count in sorted(result["counts"].items()):
            sys.stdout.write(f"    {cls}: {count}\n")
        if apply:
            sys.stdout.write(f"  applied: {len(result['applied'])}\n")
            for entry in result["applied"]:
                sys.stdout.write(f"    {entry['class']}: {entry['id']}\n")
    # A clean runtime exits 0; outstanding findings in dry-run exit 1 so CI /
    # scripts can gate on it. After a successful apply, exit reflects what
    # remains (advisory-only classes like partial_autocommit are not cleared).
    return 0 if result["total"] == 0 or (apply and result["applied"]) else 1


def cmd_doctor(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    sub = argparse.ArgumentParser(prog="agentic-os doctor", add_help=True)
    sub.add_argument("--sut", action="store_true")
    sub.add_argument("--models", action="store_true")
    sub.add_argument("--docker", action="store_true")
    sub.add_argument("--autonomy", action="store_true")
    # Issue #274 — self-healing repair scan.
    sub.add_argument(
        "--repair",
        action="store_true",
        help=(
            "Scan the runtime for drift (stale leases, orphan specs, missing "
            "NDJSON, partial autocommit, leftover pending-delete markers). "
            "Without --yes this is a DRY RUN (no mutation); safe in non-tty / "
            "autonomous contexts. Pass --yes to apply repairs."
        ),
    )
    sub.add_argument(
        "--yes",
        action="store_true",
        help="Apply repairs found by --repair (otherwise dry-run only).",
    )
    sub.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry-run for --repair (report only; the default).",
    )
    opts = sub.parse_args(args)
    if opts.repair:
        return _cmd_doctor_repair(
            repo_root,
            apply=opts.yes and not opts.dry_run,
            json_output=json_output,
            config_override=config_override,
        )
    payload = build_doctor_payload(
        repo_root,
        config_override=config_override,
        include_sut=opts.sut,
        include_models=opts.models,
        include_docker=opts.docker,
        include_autonomy=opts.autonomy,
    )
    if json_output:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        for k, v in payload.items():
            sys.stdout.write(f"{k}: {v}\n")
    # Issue #266 — `doctor --autonomy` follows the CI exit-code contract:
    # 0 ready / 2 config bad / 3 provider unavailable / 4 budget misconfigured.
    if opts.autonomy:
        autonomy = payload.get("autonomy") or {}
        return int(autonomy.get("exit_code", 0))
    return 0 if payload.get("ok") else 1


def cmd_status(repo_root: Path, args: List[str], *, json_output: bool) -> int:
    sub = argparse.ArgumentParser(prog="agentic-os status", add_help=True)
    sub.add_argument("--watch", action="store_true")
    sub.add_argument("--phase", default=None)
    sub.add_argument("--json", dest="json_local", action="store_true")
    opts = sub.parse_args(args)
    json_out = json_output or opts.json_local

    paths = runtime_paths_from_config(repo_root)
    if not paths.db.exists():
        payload = {
            "runtime": "blocked",
            "db": "missing",
            "leases": [],
            "phases": [],
            "tasks": {"queued": 0, "running": 0, "failed": 0},
            "bugs": {"open": 0, "known": 0},
            "last_run": None,
        }
        if json_out:
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        else:
            sys.stdout.write("db: missing — run scripts/agentic-os.sh init\n")
        return 0

    conn, _paths, _events, _ = open_runtime(repo_root)
    try:
        from ..storage.db import integrity_check

        tasks = fetch_task_summary(conn)
        bugs = fetch_bug_summary(conn)
        phases = fetch_phase_rows(conn)
        leases = fetch_active_leases(conn)
        last_run = fetch_last_run(conn, paths)
        blockers = list_open_blockers(conn)
        integrity = integrity_check(conn)

        runtime_state = "ready"
        if blockers:
            runtime_state = "degraded"
        if integrity != "ok":
            runtime_state = "blocked"

        payload = {
            "runtime": runtime_state,
            "db": "ok" if integrity == "ok" else "corrupt",
            "leases": leases,
            "phases": phases if not opts.phase else [p for p in phases if p["id"] == opts.phase],
            "tasks": {
                "queued": tasks["queued"],
                "leased": tasks["leased"],
                "running": tasks["running"],
                "succeeded": tasks["succeeded"],
                "failed": tasks["failed"],
                "cancelled": tasks["cancelled"],
                "timeout": tasks["timeout"],
            },
            "bugs": bugs,
            "last_run": last_run,
            "blockers_open": blockers,
        }
    finally:
        conn.close()

    if json_out:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            f"runtime: {payload['runtime']}\n"
            f"db:      {payload['db']}\n"
            f"phases:  {len(payload['phases'])} (current={CURRENT_PHASE_ID})\n"
            f"tasks:   {payload['tasks']}\n"
            f"bugs:    {payload['bugs']}\n"
            f"leases:  {len(payload['leases'])}\n"
            f"blockers_open: {len(payload['blockers_open'])}\n"
            f"last_run: {payload['last_run']}\n"
        )
    return 0


def cmd_support_bundle(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    """Build a redacted diagnostic tarball under runtime/support-bundles/.

    The CLI is a thin wrapper around `support_bundle.build_support_bundle`
    so the dashboard endpoint can reuse the same builder without parsing
    flags. JSON output mirrors the same payload the dashboard returns.
    """
    from ..support_bundle import SUPPORT_BUNDLE_SUBSYSTEMS, build_support_bundle

    sub = argparse.ArgumentParser(prog="agentic-os support-bundle", add_help=True)
    sub.add_argument(
        "--dest",
        type=Path,
        default=None,
        metavar="PATH",
        help="alternative output directory (default: <runtime>/support-bundles/)",
    )
    valid_subsystems = ",".join(sorted(SUPPORT_BUNDLE_SUBSYSTEMS))
    sub.add_argument(
        "--include",
        default=None,
        metavar="LIST",
        help=f"comma-separated subsystems to include ({valid_subsystems})",
    )
    sub.add_argument(
        "--exclude",
        default=None,
        metavar="LIST",
        help=f"comma-separated subsystems to drop from the default set ({valid_subsystems})",
    )
    sub.add_argument(
        "--no-redact",
        dest="redact",
        action="store_false",
        default=True,
        help="embed config verbatim instead of replacing secret-shaped keys with <redacted>",
    )
    sub.add_argument(
        "--tag",
        default=None,
        metavar="NAME",
        help="suffix appended to the bundle filename (alnum, dot, dash, underscore)",
    )
    opts = sub.parse_args(args)

    def _parse_list(raw: Optional[str], flag: str) -> Optional[set[str]]:
        if raw is None:
            return None
        items = {part.strip() for part in raw.split(",") if part.strip()}
        if not items:
            raise UsageError(f"{flag} requires at least one subsystem name")
        return items

    include = _parse_list(opts.include, "--include")
    exclude = _parse_list(opts.exclude, "--exclude")

    paths = runtime_paths_from_config(repo_root, override=config_override)
    paths.ensure()
    try:
        result = build_support_bundle(
            repo_root,
            paths,
            dest=opts.dest.expanduser().resolve() if opts.dest else None,
            include=include,
            exclude=exclude,
            redact=opts.redact,
            tag=opts.tag,
        )
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    if json_output:
        sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            f"support bundle written: {result['path']} ({result['bytes']} bytes)\n"
            f"  files: {len(result['manifest']['files'])}\n"
            f"  disclaimer: {result['disclaimer']}\n"
        )
    return 0


def cmd_logs(repo_root: Path, args: List[str], *, json_output: bool) -> int:
    """Tail the events log or, with --follow, stream the dashboard log.

    `--follow` is opt-in and targets the dashboard daemon log file
    (``runtime/logs/dashboard.log``), since that is the only stream
    that grows live in this codebase (issue #139). The events log is
    SQLite-backed and is read with `--lines` instead.
    """
    import time as _time

    sub = argparse.ArgumentParser(prog="agentic-os logs", add_help=True)
    sub.add_argument("--run", default=None)
    sub.add_argument("--phase", default=None)
    sub.add_argument("--follow", action="store_true")
    sub.add_argument("--lines", type=int, default=20)
    sub.add_argument(
        "--file",
        default=None,
        help="Override the file passed to --follow (default: runtime/logs/dashboard.log)",
    )
    sub.add_argument(
        "--poll-interval",
        type=float,
        default=0.2,
        help="Seconds between reads when --follow is active",
    )
    opts = sub.parse_args(args)

    paths = runtime_paths_from_config(repo_root)

    if opts.follow:
        log_path = Path(opts.file).expanduser().resolve() if opts.file else (
            paths.logs_dir / _DASHBOARD_LOGFILE_NAME
        )
        if not log_path.exists():
            raise InfraError(
                f"log file missing for --follow: {log_path}\n"
                f"start the daemon first with `agentic-os up --dashboard-only --daemon`"
            )
        # Stream the last `--lines` first so the operator has context,
        # then follow new bytes until Ctrl+C. Reading bytes (not lines)
        # lets us replay partially-written tail lines correctly on
        # the next iteration.
        with log_path.open("rb") as fh:
            buf = fh.read()
            tail = buf.decode("utf-8", errors="replace").splitlines()[-opts.lines :]
            for line in tail:
                sys.stdout.write(line + "\n")
            sys.stdout.flush()
            try:
                while True:
                    chunk = fh.read()
                    if chunk:
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.flush()
                    else:
                        _time.sleep(max(0.05, opts.poll_interval))
            except KeyboardInterrupt:
                sys.stdout.write("\n")
                return 0
        return 0

    if opts.run:
        log_path = paths.subprocess_logs_dir / f"{opts.run}.log"
        if not log_path.exists():
            raise InfraError(f"log missing for run {opts.run}: {log_path}")
        sys.stdout.write(log_path.read_text(encoding="utf-8"))
        return 0

    if not paths.events_dir.exists():
        raise InfraError("no events directory yet — run init first")

    conn, _paths, events, _ = open_runtime(repo_root)
    try:
        lines = events.tail(opts.lines)
    finally:
        conn.close()
    if json_output:
        sys.stdout.write(json.dumps(lines, indent=2, sort_keys=True) + "\n")
    else:
        for entry in lines:
            sys.stdout.write(
                f"{entry['ts']} [{entry['severity']}] {entry['kind']} :: {json.dumps(entry['payload'], sort_keys=True)}\n"
            )
    return 0
