"""Reporting commands: reports, budget, coverage (issue #292)."""

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


def cmd_coverage(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    """Issue #319 — query the per-project coverage ledger.

    Subcommands:
      list  [--project ID]                       — every covered surface + spec.
      check --kind api|ui --key KEY [--project ID]
                                                  — is a surface already covered?
    """
    from ..coverage_ledger import SURFACE_KINDS, is_covered, list_coverage
    from ..projects import DEFAULT_PROJECT_ID

    sub = argparse.ArgumentParser(prog="agentic-os coverage", add_help=True)
    sub.add_argument("action", choices=("list", "check"))
    sub.add_argument("--project", dest="project", default=DEFAULT_PROJECT_ID)
    sub.add_argument("--kind", dest="kind", default=None, choices=SURFACE_KINDS)
    sub.add_argument("--key", dest="key", default=None)
    opts = sub.parse_args(args)

    conn, _paths, _events, _orch = open_runtime(repo_root)
    try:
        if opts.action == "list":
            rows = list_coverage(conn, project_id=opts.project)
            if json_output:
                sys.stdout.write(
                    json.dumps({"coverage": rows}, indent=2, sort_keys=True) + "\n"
                )
            else:
                sys.stdout.write("surface_kind\tsurface_key\tassertion_kind\tspec_path\n")
                for r in rows:
                    sys.stdout.write(
                        f"{r['surface_kind']}\t{r['surface_key']}\t"
                        f"{r['assertion_kind']}\t{r['spec_path']}\n"
                    )
            return 0

        if opts.action == "check":
            if not opts.kind or not opts.key:
                raise UsageError("coverage check requires --kind and --key")
            covered = is_covered(
                conn,
                project_id=opts.project,
                surface_kind=opts.kind,
                surface_key=opts.key,
            )
            payload = {
                "project_id": opts.project,
                "surface_kind": opts.kind,
                "surface_key": opts.key,
                "covered": covered,
            }
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            return 0
    finally:
        conn.close()

    raise UsageError(f"unknown coverage action: {opts.action}")


def cmd_budget(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    """Issue #266 — token/USD budget view + runtime overrides.

    Subcommands: show [--session SID], set --role R --max-tokens N
    (or --session --max-tokens N), reset --session SID.
    """
    sub = argparse.ArgumentParser(prog="agentic-os budget", add_help=True)
    sub.add_argument("subcommand", choices=["show", "set", "reset"])
    sub.add_argument("--session", default=None)
    sub.add_argument("--role", default=None)
    sub.add_argument("--max-tokens", dest="max_tokens", type=int, default=None)
    sub.add_argument("--json", dest="json_flag", action="store_true")
    opts = sub.parse_args(args)
    effective_json = json_output or opts.json_flag

    from ..budgets import budget_status
    from ..config import load_or_default

    conn, paths, events, _o = open_runtime(repo_root)
    try:
        cfg = load_or_default(repo_root, override=config_override)
        budgets = (cfg.raw.get("budgets") or {}) if isinstance(cfg.raw, dict) else {}
        models = (cfg.raw.get("models") or {}) if isinstance(cfg.raw, dict) else {}

        if opts.subcommand == "show":
            payload = budget_status(conn, budgets, session_id=opts.session, models=models)
            if effective_json:
                sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
            else:
                s = payload["session"]
                sys.stdout.write(
                    f"session tokens: {s['tokens']}/{s['max_tokens']} ({s['tokens_pct']}%)\n"
                    f"session usd: {s['cost_usd']}/{s['max_usd']} ({s['usd_pct']}%)\n"
                )
                for r in payload["per_role"]:
                    sys.stdout.write(
                        f"  {r['role']:<12} {r['tokens']}/{r['max_tokens']} ({r['pct']}%)\n"
                    )
            return 0

        if opts.subcommand == "set":
            if opts.max_tokens is None:
                sys.stderr.write("set requires --max-tokens\n")
                return 64
            budgets = dict(budgets)
            if opts.role:
                if opts.role not in {"planner", "implementer", "reviewer", "triager"}:
                    sys.stderr.write(
                        "set --role must be one of planner|implementer|reviewer|triager\n"
                    )
                    return 64
                per_role = dict(budgets.get("per_role") or {})
                per_role[opts.role] = {"max_tokens": opts.max_tokens}
                budgets["per_role"] = per_role
            else:
                session = dict(budgets.get("session") or {})
                session["max_tokens"] = opts.max_tokens
                budgets["session"] = session
            new_raw = dict(cfg.raw)
            new_raw["budgets"] = budgets
            import yaml  # type: ignore[import-untyped]

            Path(cfg.source).write_text(
                yaml.safe_dump(new_raw, sort_keys=False), encoding="utf-8"
            )
            payload = {"ok": True, "budgets": budgets, "source": str(cfg.source)}
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
            return 0

        if opts.subcommand == "reset":
            if not opts.session:
                sys.stderr.write("reset requires --session SID\n")
                return 64
            with transaction(conn):
                cur = conn.execute(
                    "DELETE FROM model_invocations WHERE session_id=?;", (opts.session,)
                )
                deleted = cur.rowcount
            payload = {"ok": True, "session": opts.session, "deleted_invocations": deleted}
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
            return 0
        return 64
    finally:
        conn.close()


def cmd_reports(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    """Issue #266 — browse `reports/` artifacts from a headless box.

    Subcommands: list [--type T], show <name>, diff <a> <b>.
    """
    sub = argparse.ArgumentParser(prog="agentic-os reports", add_help=True)
    sub.add_argument("subcommand", choices=["list", "show", "diff", "html"])
    sub.add_argument("names", nargs="*")
    sub.add_argument("--type", dest="report_type", default=None)
    sub.add_argument("--output", dest="output", default=None,
                     help="target dir for `html` (default: <repo>/output)")
    sub.add_argument("--json", dest="json_flag", action="store_true")
    opts = sub.parse_args(args)
    effective_json = json_output or opts.json_flag

    # Issue #372 — (re)generate the human how-to-run guide from the template,
    # standalone (no run / no DB needed) and idempotent. Values come from the
    # project config when present, else the standalone defaults.
    if opts.subcommand == "html":
        from ..run_guide import guide_values_from_config, write_run_guide_html

        out_dir = (
            Path(opts.output).resolve()
            if opts.output
            else (repo_root / "output")
        )
        target = write_run_guide_html(
            out_dir, values=guide_values_from_config(repo_root)
        )
        if effective_json:
            sys.stdout.write(
                json.dumps({"ok": True, "guide": str(target)}, indent=2) + "\n"
            )
        else:
            sys.stdout.write(f"how-to-run.html → {target}\n")
        return 0

    reports_dir = repo_root / "reports"

    def _entries() -> List[Dict[str, Any]]:
        if not reports_dir.exists():
            return []
        out: List[Dict[str, Any]] = []
        for p in sorted(reports_dir.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
            if not p.is_file():
                continue
            if opts.report_type and not p.name.startswith(opts.report_type):
                continue
            out.append({
                "name": p.name,
                "size_bytes": p.stat().st_size,
                "modified": now_iso_from_mtime(p),
            })
        return out

    if opts.subcommand == "list":
        entries = _entries()
        if effective_json:
            sys.stdout.write(json.dumps({"reports": entries, "count": len(entries)},
                                        indent=2, sort_keys=True, default=str) + "\n")
        else:
            for e in entries:
                sys.stdout.write(f"{e['name']:<40} {e['size_bytes']:>8}B  {e['modified']}\n")
            sys.stdout.write(f"({len(entries)} report(s))\n")
        return 0

    if opts.subcommand == "show":
        if not opts.names:
            sys.stderr.write("show requires a report name\n")
            return 64
        target = (reports_dir / opts.names[0]).resolve()
        if not target.is_file() or reports_dir.resolve() not in target.parents:
            sys.stderr.write(f"report not found: {opts.names[0]}\n")
            return 4
        body = target.read_text(encoding="utf-8", errors="replace")
        sys.stdout.write(body if body.endswith("\n") else body + "\n")
        return 0

    if opts.subcommand == "diff":
        if len(opts.names) < 2:
            sys.stderr.write("diff requires two report names\n")
            return 64
        a = (reports_dir / opts.names[0]).resolve()
        b = (reports_dir / opts.names[1]).resolve()
        for p in (a, b):
            if not p.is_file() or reports_dir.resolve() not in p.parents:
                sys.stderr.write(f"report not found: {p.name}\n")
                return 4
        diff = _report_diff(a, b)
        sys.stdout.write(json.dumps(diff, indent=2, sort_keys=True, default=str) + "\n")
        return 0
    return 64


def now_iso_from_mtime(path: Path) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(
        path.stat().st_mtime, tz=_dt.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _report_diff(a: Path, b: Path) -> Dict[str, Any]:
    """Diff two JSON report manifests by their numeric fields."""
    def _load(p: Path) -> Dict[str, Any]:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    da, db = _load(a), _load(b)
    keys = sorted(set(da.keys()) | set(db.keys()))
    fields: Dict[str, Any] = {}
    for k in keys:
        va, vb = da.get(k), db.get(k)
        if isinstance(va, (int, float)) or isinstance(vb, (int, float)):
            fields[k] = {
                "a": va,
                "b": vb,
                "delta": (vb or 0) - (va or 0) if isinstance(va, (int, float)) or isinstance(vb, (int, float)) else None,
            }
        elif va != vb:
            fields[k] = {"a": va, "b": vb}
    return {"a": a.name, "b": b.name, "fields": fields}
