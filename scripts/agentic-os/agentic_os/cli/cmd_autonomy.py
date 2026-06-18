"""Autonomy & schedule commands (issue #292)."""

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
from .cmd_diagnostics import build_doctor_payload
from .cmd_lifecycle import cmd_init


def cmd_autonomy(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    """Issue #244 — CLI parity with /api/autonomy/* dashboard endpoints.

    Subcommands:
      start [--max-minutes N] [--exploratory]
      stop
      status [--json]
      preflight [--json]
      follow [--from <event-id>] [--filter kind=foo,role=planner]
    """
    sub = argparse.ArgumentParser(prog="agentic-os autonomy", add_help=True)
    sub.add_argument(
        "subcommand",
        choices=["start", "stop", "pause", "resume", "status", "preflight", "follow", "bootstrap"],
    )
    sub.add_argument("--max-minutes", type=int, default=60)
    sub.add_argument("--exploratory", action="store_true")
    sub.add_argument(
        "--no-start",
        dest="no_start",
        action="store_true",
        help="bootstrap: run init+doctor+git ensure but do not spawn the loop",
    )
    sub.add_argument("--from", dest="from_event", default=None)
    sub.add_argument(
        "--filter",
        default="",
        help="comma-separated key=value pairs (kind, role, work_item_id, severity)",
    )
    sub.add_argument("--json", dest="json_flag", action="store_true")
    opts = sub.parse_args(args)
    effective_json = json_output or opts.json_flag

    from .. import autonomy as _autonomy

    conn, paths, events, _cfg = open_runtime(repo_root)
    try:
        if opts.subcommand == "preflight":
            payload = _autonomy.preflight_check(paths)
            return _emit_autonomy_payload(payload, effective_json)
        if opts.subcommand == "status":
            payload = _autonomy.current_status()
            return _emit_autonomy_payload(payload, effective_json)
        if opts.subcommand == "start":
            preflight = _autonomy.preflight_check(paths)
            if not preflight.get("ok", False):
                sys.stderr.write("preflight failed; refusing to start autonomy session\n")
                return _emit_autonomy_payload(preflight, effective_json) or 2
            state = _autonomy.start_session(paths, max_minutes=opts.max_minutes)
            payload = {
                "session_id": getattr(state, "session_id", None),
                "started_at": getattr(state, "started_at", None),
                "max_minutes": opts.max_minutes,
                "exploratory": opts.exploratory,
                "active": _autonomy.is_session_active(),
            }
            return _emit_autonomy_payload(payload, effective_json)
        if opts.subcommand == "stop":
            payload = _autonomy.stop_session()
            return _emit_autonomy_payload(payload, effective_json)
        if opts.subcommand == "pause":
            payload = _autonomy.pause_session()
            return _emit_autonomy_payload(payload, effective_json)
        if opts.subcommand == "resume":
            payload = _autonomy.resume_session()
            return _emit_autonomy_payload(payload, effective_json)
        if opts.subcommand == "bootstrap":
            return _autonomy_bootstrap(
                repo_root,
                paths,
                max_minutes=opts.max_minutes,
                no_start=opts.no_start,
                json_output=effective_json,
                config_override=config_override,
            )
        if opts.subcommand == "follow":
            return _autonomy_follow(
                paths,
                from_event=opts.from_event,
                filter_spec=opts.filter,
                json_output=effective_json,
            )
        sys.stderr.write(f"unknown autonomy subcommand: {opts.subcommand}\n")
        return 64
    finally:
        conn.close()


def _emit_autonomy_payload(payload: Dict[str, Any], json_output: bool) -> int:
    if json_output:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    else:
        for key, value in sorted(payload.items()):
            sys.stdout.write(f"{key}: {value}\n")
    return 0


def _autonomy_follow(
    paths: RuntimePaths,
    *,
    from_event: Optional[str],
    filter_spec: str,
    json_output: bool,
) -> int:
    """Tail the NDJSON events log, optionally filtering by key=value pairs."""
    import time as _time

    filters: Dict[str, set] = {}
    if filter_spec:
        for token in filter_spec.split(","):
            token = token.strip()
            if not token or "=" not in token:
                continue
            key, _, value = token.partition("=")
            key = key.strip()
            value = value.strip()
            if not key or not value:
                continue
            filters.setdefault(key, set()).add(value)

    def _matches(row: Dict[str, Any]) -> bool:
        if not filters:
            return True
        payload = row.get("payload") or {}
        for key, allowed in filters.items():
            if key in row:
                candidate = str(row.get(key, ""))
            elif isinstance(payload, dict) and key in payload:
                candidate = str(payload.get(key, ""))
            else:
                return False
            # Support kind=step.* glob prefix.
            ok = False
            for v in allowed:
                if v.endswith("*"):
                    if candidate.startswith(v[:-1]):
                        ok = True
                        break
                elif candidate == v:
                    ok = True
                    break
            if not ok:
                return False
        return True

    events_dir = paths.events_dir
    if not events_dir.exists():
        raise InfraError("no events directory yet — run `agentic-os init` first")

    seen_after_marker = from_event is None
    poll_interval = 1.0
    last_offsets: Dict[Path, int] = {}

    try:
        while True:
            files = sorted(events_dir.glob("*.ndjson"))
            for path in files:
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                start = last_offsets.get(path, 0)
                if start >= size:
                    continue
                with path.open("rb") as fh:
                    fh.seek(start)
                    chunk = fh.read()
                last_offsets[path] = size
                for raw in chunk.decode("utf-8", errors="replace").splitlines():
                    if not raw.strip():
                        continue
                    try:
                        row = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not seen_after_marker:
                        if row.get("id") == from_event:
                            seen_after_marker = True
                        continue
                    if not _matches(row):
                        continue
                    if json_output:
                        sys.stdout.write(json.dumps(row, sort_keys=True) + "\n")
                    else:
                        sys.stdout.write(
                            f"{row.get('ts','')} [{row.get('severity','info')}] "
                            f"{row.get('kind','?')} :: {json.dumps(row.get('payload', {}), sort_keys=True)}\n"
                        )
                    sys.stdout.flush()
            _time.sleep(poll_interval)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return 0


def _autonomy_bootstrap(
    repo_root: Path,
    paths: RuntimePaths,
    *,
    max_minutes: int,
    no_start: bool,
    json_output: bool,
    config_override: Optional[Path],
) -> int:
    """Issue #266 — one-shot onboarding for a fresh SUT directory.

    Runs init → doctor --autonomy (gate) → git ensure (when enabled) →
    start. Idempotent. With ``--no-start`` it stops after the readiness
    gate so CI can validate the wiring without launching the loop.
    Exit codes follow `doctor --autonomy`: 2 config / 3 provider / 4 budget.
    """
    import contextlib
    import io as _io

    steps: List[Dict[str, Any]] = []

    # 1. init — idempotent; creates runtime + config example. Its human
    # banner is swallowed so bootstrap's own (JSON) output stays parseable.
    with contextlib.redirect_stdout(_io.StringIO()):
        init_rc = cmd_init(repo_root, [], json_output=False)
    steps.append({"step": "init", "exit_code": init_rc, "ok": init_rc == 0})
    if init_rc != 0:
        return _emit_bootstrap(steps, 1, json_output)

    # 2. doctor --autonomy — must be green before we touch git or start.
    doctor = build_doctor_payload(
        repo_root,
        config_override=config_override,
        include_autonomy=True,
    )
    autonomy_doctor = doctor.get("autonomy") or {}
    doctor_exit = int(autonomy_doctor.get("exit_code", 0))
    steps.append({"step": "doctor", "exit_code": doctor_exit, "ok": doctor_exit == 0,
                  "detail": autonomy_doctor})
    if doctor_exit != 0:
        return _emit_bootstrap(steps, doctor_exit, json_output)

    # 3. git ensure — only when git.enabled in config.
    from ..config import load_or_default

    cfg = load_or_default(repo_root, override=config_override)
    git_cfg = (cfg.raw.get("git") or {}) if isinstance(cfg.raw, dict) else {}
    if git_cfg.get("enabled"):
        from ..sut_repo import git_ensure

        conn, gpaths, events, _o = open_runtime(repo_root)
        try:
            report = git_ensure(
                gpaths,
                events,
                git_config=git_cfg,
                sut_root=(cfg.raw.get("sut") or {}).get("root") or ".",
            )
        finally:
            conn.close()
        steps.append({"step": "git_ensure", "ok": report.ok, "summary": report.summary})
        # A failed git ensure means the SUT repo is not prepared — do not start
        # autonomy against it. Bootstrap is a gate, not a best-effort pass.
        if not report.ok:
            return _emit_bootstrap(steps, 2, json_output)
    else:
        steps.append({"step": "git_ensure", "ok": True, "skipped": "git.enabled=false"})

    # 4. start — in-process loop unless --no-start.
    if no_start:
        steps.append({"step": "start", "ok": True, "skipped": "--no-start"})
        return _emit_bootstrap(steps, 0, json_output)

    from .. import autonomy as _autonomy

    state = _autonomy.start_session(paths, max_minutes=max_minutes)
    steps.append({"step": "start", "ok": True, "session_id": getattr(state, "session_id", None)})
    return _emit_bootstrap(steps, 0, json_output)


def _emit_bootstrap(steps: List[Dict[str, Any]], exit_code: int, json_output: bool) -> int:
    if json_output:
        sys.stdout.write(json.dumps(
            {"ok": exit_code == 0, "exit_code": exit_code, "steps": steps},
            indent=2, sort_keys=True, default=str,
        ) + "\n")
    else:
        for step in steps:
            mark = "ok  " if step.get("ok") else "fail"
            sys.stdout.write(f"[{mark}] {step.get('step')}\n")
        sys.stdout.write(f"bootstrap exit_code={exit_code}\n")
    return exit_code


def cmd_schedule(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    """Manage cron-style schedules for autonomous runs (issue #271)."""
    from ..scheduler import (
        CronError,
        add_schedule,
        get_schedule,
        list_schedules,
        remove_schedule,
        run_now,
        set_enabled,
    )

    sub = argparse.ArgumentParser(prog="agentic-os schedule", add_help=True)
    sub.add_argument(
        "action",
        choices=("add", "list", "remove", "enable", "disable", "run-now"),
    )
    sub.add_argument("name", nargs="?")
    sub.add_argument("--cron", dest="cron", default=None)
    sub.add_argument("--action", dest="cmd_action", default=None)
    sub.add_argument(
        "--disabled",
        dest="disabled",
        action="store_true",
        help="Create the schedule disabled (default: enabled).",
    )
    opts = sub.parse_args(args)

    conn, paths, events, _orch = open_runtime(repo_root)
    try:
        if opts.action == "add":
            if not opts.name:
                raise UsageError("schedule add requires a <name>")
            if not opts.cron:
                raise UsageError("schedule add requires --cron \"<expr>\"")
            if not opts.cmd_action:
                raise UsageError("schedule add requires --action \"<agentic-os args>\"")
            try:
                sched = add_schedule(
                    conn,
                    name=opts.name,
                    cron=opts.cron,
                    action=opts.cmd_action,
                    enabled=not opts.disabled,
                )
            except CronError as exc:
                raise UsageError(f"invalid cron expression: {exc}")
            if json_output:
                sys.stdout.write(json.dumps(sched.as_dict(), indent=2, sort_keys=True) + "\n")
            else:
                sys.stdout.write(
                    f"schedule added: {sched.name}\n"
                    f"  cron:    {sched.cron}\n"
                    f"  action:  {sched.action}\n"
                    f"  enabled: {sched.enabled}\n"
                )
            return 0

        if opts.action == "list":
            scheds = list_schedules(conn)
            if json_output:
                sys.stdout.write(
                    json.dumps(
                        {"schedules": [s.as_dict() for s in scheds]},
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                )
            elif not scheds:
                sys.stdout.write("(no schedules)\n")
            else:
                sys.stdout.write("name\tenabled\tcron\tnext_fire\tlast_run\tlast_status\taction\n")
                for s in scheds:
                    d = s.as_dict()
                    sys.stdout.write(
                        f"{s.name}\t{'on' if s.enabled else 'off'}\t{s.cron}\t"
                        f"{d.get('next_fire') or '-'}\t{s.last_run or '-'}\t"
                        f"{s.last_status or '-'}\t{s.action}\n"
                    )
            return 0

        if opts.action == "remove":
            if not opts.name:
                raise UsageError("schedule remove requires a <name>")
            removed = remove_schedule(conn, opts.name)
            if not removed:
                raise UsageError(f"unknown schedule: {opts.name}")
            if json_output:
                sys.stdout.write(json.dumps({"removed": opts.name}, sort_keys=True) + "\n")
            else:
                sys.stdout.write(f"schedule removed: {opts.name}\n")
            return 0

        if opts.action in {"enable", "disable"}:
            if not opts.name:
                raise UsageError(f"schedule {opts.action} requires a <name>")
            updated = set_enabled(conn, opts.name, opts.action == "enable")
            if not updated:
                raise UsageError(f"unknown schedule: {opts.name}")
            if json_output:
                sys.stdout.write(
                    json.dumps(
                        {"name": opts.name, "enabled": opts.action == "enable"},
                        sort_keys=True,
                    )
                    + "\n"
                )
            else:
                sys.stdout.write(f"schedule {opts.name} {opts.action}d\n")
            return 0

        if opts.action == "run-now":
            if not opts.name:
                raise UsageError("schedule run-now requires a <name>")
            if get_schedule(conn, opts.name) is None:
                raise UsageError(f"unknown schedule: {opts.name}")
            payload = run_now(conn, events, paths, opts.name)
            if json_output:
                sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            else:
                sys.stdout.write(
                    f"schedule fired: {opts.name}\n"
                    f"  status: {payload.get('status')}\n"
                    f"  pid:    {payload.get('pid')}\n"
                )
            return 0
    finally:
        conn.close()

    raise UsageError(f"unknown schedule action: {opts.action}")
