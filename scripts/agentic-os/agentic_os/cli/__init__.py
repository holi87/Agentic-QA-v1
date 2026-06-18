"""Command-line entrypoint — see docs/cli-contract.md.

Decomposed in issue #292: command families live in `cli.cmd_*` submodules.
This module keeps the argparse dispatcher and re-exports every public symbol
so existing imports (`from agentic_os.cli import cmd_foo`) keep working.
"""
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

from .cmd_autonomy import _autonomy_bootstrap, _autonomy_follow, _emit_autonomy_payload, _emit_bootstrap, cmd_autonomy, cmd_schedule
from .cmd_diagnostics import _build_autonomy_doctor, _cmd_doctor_repair, build_doctor_payload, cmd_doctor, cmd_logs, cmd_status, cmd_support_bundle
from .cmd_docs import _validated_config_sut_root, cmd_crawler, cmd_inbox
from .cmd_lifecycle import _DASHBOARD_LOGFILE_NAME, _DASHBOARD_PIDFILE_NAME, _dashboard_log_path, _dashboard_pid_path, _install_agentic_os_shim, _install_sample_sut, _prepare_init_config, _process_alive, _run_sut_workflow, _spawn_dashboard_daemon, cmd_down, cmd_init, cmd_migrate_runtime, cmd_run, cmd_up
from .cmd_project import cmd_git, cmd_notifications, cmd_project
from .cmd_reporting import _report_diff, cmd_budget, cmd_coverage, cmd_reports, now_iso_from_mtime
from .cmd_sessions import cmd_learnings, cmd_memory, cmd_sessions, cmd_transcripts, cmd_verifications
from .cmd_workflow import cmd_task


HELP_TEXT = """Agentic OS — agentic web testing orchestrator (QualityCat output layer).

Commands:
  init            Bootstrap agentic-os-runtime/ state and config example.
                  --install-shim [--shim-dir DIR] drops ~/.local/bin/agentic-os.
  doctor          Sanity checks; exits non-zero on issues (issue #96).
  up              Start dashboard + autonomy session (orchestrator daemon).
                  --dashboard-only keeps the read-only console without autonomy.
                  --daemon detaches; pidfile in runtime/pids/dashboard.pid.
  down            Stop the dashboard daemon (SIGTERM then SIGKILL fallback).
  run             Workflows: dry-run [--fake-sut], recovery, run-tests, review-gate, final-gate.
  task            Manage operator-level work items and candidate approvals.
  inbox           List, ingest, or synthesize documents from ./inbox/ and ./pretask/.
  status          Show runtime status (use --json for machine output).
  logs            Tail agentic-os-runtime/events; --follow streams dashboard.log.
  crawler         Same-origin route crawler for exploratory tests (issue #136).
  migrate-runtime Move legacy .agentic-os/ to agentic-os-runtime/ (issue #142).
  support-bundle  Redacted diagnostic tarball under runtime/support-bundles/ (issue #146).
  autonomy        Headless control of the autonomy session — parity with the
                  dashboard endpoints. Subcommands: start, stop, pause, resume,
                  status, preflight, follow, bootstrap (issues #244, #266).
  verifications   Reviewer/triager decision trail: list, show, override (#266).
  budget          Token/USD budget: show, set, reset (#266).
  reports         Browse reports/ artifacts: list, show, diff (#266).
  notifications   Push alerts on blocked/budget/failover/completed; test (#268).
  transcripts     Show a model invocation's reasoning transcript (#270).
  git             Idempotent SUT git bootstrap from `config/agentic-os.yml`
                  (issue #241). Subcommand: ensure.
  schedule        Cron-style schedules for autonomous runs (issue #271).
                  Subcommands: add, list, remove, enable, disable, run-now.
  sessions        Autonomy session artifacts. Subcommand: summary <id> [--json]
                  prints the PR-ready handoff doc (issue #272).
  project         Addressable projects over the flat work-item list (#288).
                  Subcommands: list, show <id>, register <name> [--sut-root P]
                  [--id SLUG]. Active project: config `project.active` or default.
  coverage        Per-project coverage ledger of covered surfaces (#319).
                  Subcommands: list [--project ID]; check --kind api|ui --key KEY
                  [--project ID] answers "is surface X already covered?".

Notes:
  - `up --daemon` detaches the dashboard only (issue #139). The full
    orchestrator daemon — autonomous task scheduling, lease handoff,
    background phase machine — is NOT in this release (issue #83).
    Use `up --daemon` for the dashboard process and stop it with `down`.
  - `run dry-run --fake-sut` is the supported onboarding proof fixture
    (issue #73).

Compatibility aliases (extra flags pass through to the target command):
  serve [...flags]  -> up --foreground --dashboard-only [...flags]
                       e.g. `serve --full` enables write endpoints for the session.
  start    -> up
  resume   -> run recovery; up --foreground
  dry-run  -> run dry-run

Global options:
  --config <path>  override config location (default config/agentic-os.yml)
  --root <path>    override repo root
  --json           machine-readable output where supported
  --verbose        verbose stderr
  --no-color       plain text output
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic-os", add_help=False)
    parser.add_argument("--config", dest="config", default=None)
    parser.add_argument("--root", dest="root", default=None)
    parser.add_argument("--json", dest="json", action="store_true")
    parser.add_argument("--verbose", dest="verbose", action="store_true")
    parser.add_argument("--no-color", dest="no_color", action="store_true")
    parser.add_argument("--help", "-h", dest="help", action="store_true")
    parser.add_argument("command", nargs="?")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def _resolve_repo_root(arg: Optional[str]) -> Path:
    if arg:
        candidate = Path(arg).expanduser().resolve()
        if not candidate.exists():
            raise UsageError(f"--root path does not exist: {candidate}")
        return candidate
    return detect_repo_root(Path.cwd())


def _alias(command: Optional[str], rest: List[str]) -> List[str]:
    if command == "serve":
        return ["up", "--foreground", "--dashboard-only", *rest]
    if command == "start":
        return ["up", *rest]
    if command == "resume":
        return ["__resume__", *rest]
    if command == "dry-run":
        return ["run", "dry-run", *rest]
    return [command, *rest] if command else []


def main(argv: Sequence[str]) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)

    if ns.help and not ns.command:
        sys.stdout.write(HELP_TEXT)
        return 0

    if not ns.command:
        sys.stdout.write(HELP_TEXT)
        return 64

    try:
        repo_root = _resolve_repo_root(ns.root)
    except UsageError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return exc.exit_code

    chain = _alias(ns.command, list(ns.args or []))
    command = chain[0]
    rest = chain[1:]

    config_path = Path(ns.config).expanduser().resolve() if ns.config else None

    # Diagnostic banner goes to stderr so `--json` stdout stays parseable
    # from byte 1. Humans still see the line in interactive use.
    sys.stderr.write(
        f"agentic-os {command} (repo={repo_root}, config={config_path or '<default>'} )\n"
    )

    # Issue #77 — thread the global `--config <path>` override into
    # every command handler so `doctor`, `run`, `task`, `up` and the
    # rest actually load the operator-supplied config instead of
    # silently falling back to the canonical repo file.
    _ACTIVE_CONFIG_OVERRIDE["path"] = config_path
    from ..config import set_active_config_override

    set_active_config_override(config_path)

    try:
        if command == "init":
            return cmd_init(repo_root, rest, json_output=ns.json)
        if command == "doctor":
            return cmd_doctor(repo_root, rest, json_output=ns.json, config_override=config_path)
        if command == "up":
            return cmd_up(repo_root, rest, json_output=ns.json, config_override=config_path)
        if command == "down":
            return cmd_down(repo_root, rest, json_output=ns.json)
        if command == "run":
            return cmd_run(repo_root, rest, json_output=ns.json, config_override=config_path)
        if command == "task":
            return cmd_task(repo_root, rest, json_output=ns.json, config_override=config_path)
        if command == "inbox":
            return cmd_inbox(repo_root, rest, json_output=ns.json)
        if command == "status":
            return cmd_status(repo_root, rest, json_output=ns.json)
        if command == "logs":
            return cmd_logs(repo_root, rest, json_output=ns.json)
        if command == "crawler":
            return cmd_crawler(repo_root, rest, json_output=ns.json)
        if command == "migrate-runtime":
            return cmd_migrate_runtime(repo_root, rest, json_output=ns.json)
        if command == "support-bundle":
            return cmd_support_bundle(repo_root, rest, json_output=ns.json, config_override=config_path)
        if command == "autonomy":
            return cmd_autonomy(repo_root, rest, json_output=ns.json, config_override=config_path)
        if command == "verifications":
            return cmd_verifications(repo_root, rest, json_output=ns.json, config_override=config_path)
        if command == "budget":
            return cmd_budget(repo_root, rest, json_output=ns.json, config_override=config_path)
        if command == "reports":
            return cmd_reports(repo_root, rest, json_output=ns.json, config_override=config_path)
        if command == "notifications":
            return cmd_notifications(repo_root, rest, json_output=ns.json, config_override=config_path)
        if command == "transcripts":
            return cmd_transcripts(repo_root, rest, json_output=ns.json, config_override=config_path)
        if command == "git":
            return cmd_git(repo_root, rest, json_output=ns.json, config_override=config_path)
        if command == "schedule":
            return cmd_schedule(repo_root, rest, json_output=ns.json, config_override=config_path)
        if command == "sessions":
            return cmd_sessions(repo_root, rest, json_output=ns.json, config_override=config_path)
        if command == "learnings":
            return cmd_learnings(repo_root, rest, json_output=ns.json, config_override=config_path)
        if command == "memory":
            return cmd_memory(repo_root, rest, json_output=ns.json, config_override=config_path)
        if command == "project":
            return cmd_project(repo_root, rest, json_output=ns.json, config_override=config_path)
        if command == "coverage":
            return cmd_coverage(repo_root, rest, json_output=ns.json, config_override=config_path)
        if command == "__resume__":
            rc = cmd_run(repo_root, ["recovery", *rest], json_output=ns.json, config_override=config_path)
            if rc != 0:
                return rc
            return cmd_up(repo_root, ["--foreground", *rest], json_output=ns.json, config_override=config_path)
        sys.stderr.write(f"error: unknown command '{command}'\ntry: scripts/agentic-os.sh --help\n")
        return 64
    except UsageError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return exc.exit_code
    except ConfigError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return exc.exit_code
    except InfraError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return exc.exit_code
    except ProductFailure as exc:
        sys.stderr.write(f"{exc}\n")
        return exc.exit_code
    except UserAbort:
        sys.stderr.write("aborted by operator\n")
        return 130
    except AgenticOSError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return exc.exit_code
    except KeyboardInterrupt:
        sys.stderr.write("aborted by operator\n")
        return 130


# `_ACTIVE_CONFIG_OVERRIDE` and `_active_config_override` live in `cli._state`
# so every `cli.cmd_*` submodule can read them without importing back into
# this package (issue #292).
from ._state import _ACTIVE_CONFIG_OVERRIDE, _active_config_override  # noqa: E402,F401
