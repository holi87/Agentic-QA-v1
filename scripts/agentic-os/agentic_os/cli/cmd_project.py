"""Project commands: project, git, notifications (issue #292)."""

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


def cmd_notifications(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    """Issue #268 — `agentic-os notifications test --channel <name>`."""
    sub = argparse.ArgumentParser(prog="agentic-os notifications", add_help=True)
    sub.add_argument("subcommand", choices=["test"])
    sub.add_argument("--channel", required=True, choices=["webhook", "desktop", "sound"])
    sub.add_argument("--json", dest="json_flag", action="store_true")
    opts = sub.parse_args(args)
    effective_json = json_output or opts.json_flag

    from ..config import load_or_default
    from ..notifications import send_test

    cfg = load_or_default(repo_root, override=config_override)
    paths = runtime_paths_from_config(repo_root, override=config_override)
    result = send_test(cfg.raw, opts.channel, paths=paths)
    if effective_json:
        sys.stdout.write(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")
    else:
        mark = "ok" if result.get("ok") else "fail"
        sys.stdout.write(f"[{mark}] notifications test channel={opts.channel}"
                         + (f" — {result.get('error')}" if not result.get("ok") else "") + "\n")
    return 0 if result.get("ok") else 1


def cmd_git(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    """Issue #241 — `agentic-os git ensure` applies the declarative `git:`
    block from config/agentic-os.yml. Idempotent."""
    sub = argparse.ArgumentParser(prog="agentic-os git", add_help=True)
    sub.add_argument("subcommand", choices=["ensure"])
    sub.add_argument("--json", dest="json_flag", action="store_true")
    opts = sub.parse_args(args)
    effective_json = json_output or opts.json_flag

    from ..config import ConfigError, load_or_default
    from ..sut_repo import git_ensure

    try:
        cfg = load_or_default(repo_root)
    except ConfigError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return exc.exit_code if hasattr(exc, "exit_code") else 64

    git_cfg = (cfg.raw.get("git") or {}) if isinstance(cfg.raw, dict) else {}
    sut_root = (cfg.raw.get("sut") or {}).get("root") or "."

    conn, paths, events, _cfg = open_runtime(repo_root)
    try:
        report = git_ensure(
            paths,
            events,
            git_config=git_cfg,
            sut_root=sut_root,
        )
    finally:
        conn.close()

    if effective_json:
        sys.stdout.write(json.dumps({
            "ok": report.ok,
            "summary": report.summary,
            "ops": list(report.ops),
        }, indent=2, sort_keys=True, default=str) + "\n")
        return 0 if report.ok else 1

    sys.stdout.write(f"git binary on PATH: {shutil.which('git') or 'missing'}\n")
    sys.stdout.write(f"sut.root: {sut_root}\n")
    for op in report.ops:
        mark = "ok  " if op.get("ok") else "fail"
        detail = op.get("detail") or {}
        sys.stdout.write(f"[{mark}] {op.get('op')}: {detail}\n")
    sys.stdout.write(f"git ensure: {report.summary}\n")
    return 0 if report.ok else 1


def cmd_project(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    """Issue #288 — inspect / register addressable projects.

    Subcommands: list, show <id>, register <name> [--sut-root P] [--id SLUG].
    The active project is resolved from `project.active` in config (else the
    `default` project); selecting it persistently is deferred to a follow-up.
    """
    from ..projects import get_project, list_projects, register_project

    sub = argparse.ArgumentParser(prog="agentic-os project", add_help=True)
    sub.add_argument("action", choices=("list", "show", "register"))
    sub.add_argument("name", nargs="?", default=None, help="project id (show) or name (register)")
    sub.add_argument("--sut-root", dest="sut_root", default=".")
    sub.add_argument("--id", dest="project_id", default=None)
    opts = sub.parse_args(args)

    conn, _paths, _events, _orch = open_runtime(repo_root)
    try:
        if opts.action == "list":
            rows = list_projects(conn)
            if json_output:
                sys.stdout.write(json.dumps({"projects": rows}, indent=2, sort_keys=True) + "\n")
            else:
                sys.stdout.write("id\tname\tsut_root\tcreated_at\n")
                for r in rows:
                    sys.stdout.write(f"{r['id']}\t{r['name']}\t{r['sut_root']}\t{r['created_at']}\n")
            return 0

        if opts.action == "show":
            if not opts.name:
                raise UsageError("project show requires an <id>")
            row = get_project(conn, opts.name)
            if row is None:
                raise UsageError(f"unknown project: {opts.name}")
            sys.stdout.write(json.dumps(row, indent=2, sort_keys=True) + "\n")
            return 0

        if opts.action == "register":
            if not opts.name:
                raise UsageError("project register requires a <name>")
            row = register_project(
                conn, name=opts.name, sut_root=opts.sut_root, project_id=opts.project_id
            )
            if json_output:
                sys.stdout.write(json.dumps({"registered": row}, indent=2, sort_keys=True) + "\n")
            else:
                sys.stdout.write(f"project registered: {row['id']} (sut_root={row['sut_root']})\n")
            return 0
    finally:
        conn.close()

    raise UsageError(f"unknown project action: {opts.action}")
