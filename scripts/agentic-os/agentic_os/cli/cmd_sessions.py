"""Session & memory commands: sessions, transcripts, verifications, learnings, memory (issue #292)."""

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


def cmd_transcripts(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    """Issue #270 — `agentic-os transcripts show <invocation-id>`."""
    sub = argparse.ArgumentParser(prog="agentic-os transcripts", add_help=True)
    sub.add_argument("subcommand", choices=["show"])
    sub.add_argument("invocation_id")
    sub.add_argument("--json", dest="json_flag", action="store_true")
    opts = sub.parse_args(args)
    effective_json = json_output or opts.json_flag

    from ..transcripts import get_transcript

    conn, paths, events, _o = open_runtime(repo_root)
    try:
        chunks = get_transcript(conn, opts.invocation_id)
    finally:
        conn.close()
    if not chunks:
        sys.stderr.write(f"no transcript for invocation: {opts.invocation_id}\n")
        return 4
    if effective_json:
        sys.stdout.write(json.dumps({"invocation_id": opts.invocation_id, "chunks": chunks},
                                    indent=2, sort_keys=True, default=str) + "\n")
    else:
        for c in chunks:
            sys.stdout.write(f"[{c['ord']}] {c['kind']}\n{c['payload']}\n\n")
    return 0


def cmd_sessions(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    """Issue #272 — session summary artifact from a headless box.

    Subcommand: summary <session_id> [--json] prints the PR-ready handoff doc.
    """
    from ..summaries import build_session_summary, render_session_summary

    sub = argparse.ArgumentParser(prog="agentic-os sessions", add_help=True)
    sub.add_argument("subcommand", choices=["summary"])
    sub.add_argument("session_id", nargs="?", default=None)
    sub.add_argument("--json", dest="json_flag", action="store_true")
    opts = sub.parse_args(args)
    effective_json = json_output or opts.json_flag

    if not opts.session_id:
        raise UsageError("sessions summary requires a <session_id>")

    conn, _paths, _events, _o = open_runtime(repo_root)
    try:
        if effective_json:
            data = build_session_summary(conn, opts.session_id)
            if data is None:
                sys.stderr.write(f"session not found: {opts.session_id}\n")
                return 4
            sys.stdout.write(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")
            return 0
        markdown = render_session_summary(conn, opts.session_id)
        if markdown is None:
            sys.stderr.write(f"session not found: {opts.session_id}\n")
            return 4
        sys.stdout.write(markdown)
        return 0
    finally:
        conn.close()


def cmd_learnings(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    """Issue #273 — inspect / prune the cross-run learnings store.

    Subcommands: list [--kind|--subject|--limit], show <id>, forget <id>
    (operator override), decay [--tau-days|--min-weight] (fired nightly by the
    scheduler, e.g. `schedule add learnings-decay --cron "0 3 * * *"
    --action "learnings decay"`).
    """
    from ..learnings import (
        VALID_KINDS,
        decay_learnings,
        forget_learning,
        get_learning,
        list_learnings,
    )

    sub = argparse.ArgumentParser(prog="agentic-os learnings", add_help=True)
    sub.add_argument("action", choices=("list", "show", "forget", "decay"))
    sub.add_argument("id", nargs="?", default=None)
    sub.add_argument("--kind", choices=VALID_KINDS, default=None)
    sub.add_argument("--subject", default=None)
    sub.add_argument("--limit", type=int, default=50)
    sub.add_argument("--tau-days", dest="tau_days", type=float, default=None)
    sub.add_argument("--min-weight", dest="min_weight", type=float, default=None)
    opts = sub.parse_args(args)

    conn, _paths, _events, _orch = open_runtime(repo_root)
    try:
        if opts.action == "list":
            rows = list_learnings(
                conn, kind=opts.kind, subject=opts.subject, limit=opts.limit
            )
            if json_output:
                sys.stdout.write(json.dumps({"learnings": rows}, indent=2, sort_keys=True) + "\n")
            elif not rows:
                sys.stdout.write("(no learnings)\n")
            else:
                sys.stdout.write("id\tkind\tweight\tobserved_at\tsubject\n")
                for r in rows:
                    sys.stdout.write(
                        f"{r['id']}\t{r['kind']}\t{r['weight']:.3f}\t"
                        f"{r['observed_at']}\t{r['subject']}\n"
                    )
            return 0

        if opts.action == "show":
            if not opts.id:
                raise UsageError("learnings show requires an <id>")
            row = get_learning(conn, int(opts.id))
            if row is None:
                raise UsageError(f"unknown learning: {opts.id}")
            sys.stdout.write(json.dumps(row, indent=2, sort_keys=True) + "\n")
            return 0

        if opts.action == "forget":
            if not opts.id:
                raise UsageError("learnings forget requires an <id>")
            removed = forget_learning(conn, int(opts.id))
            if not removed:
                raise UsageError(f"unknown learning: {opts.id}")
            if json_output:
                sys.stdout.write(json.dumps({"forgotten": int(opts.id)}, sort_keys=True) + "\n")
            else:
                sys.stdout.write(f"learning forgotten: {opts.id}\n")
            return 0

        if opts.action == "decay":
            kwargs: Dict[str, Any] = {}
            if opts.tau_days is not None:
                kwargs["tau_days"] = opts.tau_days
            if opts.min_weight is not None:
                kwargs["min_weight"] = opts.min_weight
            res = decay_learnings(conn, **kwargs)
            if json_output:
                sys.stdout.write(json.dumps(res, sort_keys=True) + "\n")
            else:
                sys.stdout.write(
                    f"decay: {res['recomputed']} recomputed, {res['pruned']} pruned\n"
                )
            return 0
    finally:
        conn.close()

    raise UsageError(f"unknown learnings action: {opts.action}")


def cmd_memory(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    """Issue #289 — per-project RAG memory.

    Subcommands:
      build              re-index the active project's history into memory_index.
      query <text>       return ranked prior-context snippets for the active project.

    The active project is resolved via `projects.resolve_active_project_id`
    (explicit `--project` > config `project.active` > the `default` project),
    so both subcommands stay scoped — they never mix projects.
    """
    from .. import memory as _memory
    from ..projects import resolve_active_project_id

    sub = argparse.ArgumentParser(prog="agentic-os memory", add_help=True)
    sub.add_argument("action", choices=("build", "query"))
    sub.add_argument("text", nargs="?", default=None, help="query text (query)")
    sub.add_argument("--project", dest="project", default=None)
    sub.add_argument("--limit", type=int, default=5)
    opts = sub.parse_args(args)

    conn, paths, events, _orch = open_runtime(repo_root)
    try:
        # Config drives `project.active`; load best-effort so a missing/partial
        # config still resolves to the default project rather than erroring.
        cfg = None
        try:
            from ..config import load_or_default

            cfg = load_or_default(repo_root)
        except Exception:
            cfg = None
        project_id = resolve_active_project_id(conn, cfg, explicit=opts.project)

        if opts.action == "build":
            counts = _memory.build_memory(
                conn, paths, project_id=project_id, events=events
            )
            if json_output:
                sys.stdout.write(
                    json.dumps(
                        {"project_id": project_id, "counts": counts},
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                )
            else:
                total = sum(counts.values())
                detail = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                sys.stdout.write(
                    f"memory build [{project_id}]: {total} indexed ({detail})\n"
                )
            return 0

        if opts.action == "query":
            if not opts.text:
                raise UsageError("memory query requires <text>")
            results = _memory.query_memory(
                conn, project_id=project_id, text=opts.text, limit=opts.limit
            )
            if json_output:
                sys.stdout.write(
                    json.dumps(
                        {"project_id": project_id, "results": results},
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                )
            elif not results:
                sys.stdout.write("(no matches)\n")
            else:
                sys.stdout.write("source\tsource_id\tscore\ttitle\n")
                for r in results:
                    sys.stdout.write(
                        f"{r['source']}\t{r['source_id']}\t{r['score']:.3f}\t{r['title']}\n"
                    )
            return 0
    finally:
        conn.close()

    raise UsageError(f"unknown memory action: {opts.action}")


def cmd_verifications(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    """Issue #266 — headless access to the reviewer/triager decision trail.

    Subcommands: list [--work-item|--actor|--limit], show DEC-ID,
    override DEC-ID --severity Sx [--reason ...].
    """
    sub = argparse.ArgumentParser(prog="agentic-os verifications", add_help=True)
    sub.add_argument("subcommand", choices=["list", "show", "override"])
    sub.add_argument("decision_id", nargs="?", default=None)
    sub.add_argument("--actor", default=None)
    sub.add_argument("--work-item", dest="work_item", default=None)
    sub.add_argument("--limit", type=int, default=50)
    sub.add_argument("--severity", default=None)
    sub.add_argument("--reason", default="")
    sub.add_argument("--json", dest="json_flag", action="store_true")
    opts = sub.parse_args(args)
    effective_json = json_output or opts.json_flag

    from ..decisions import fetch_decisions, get_decision, record_decision

    conn, paths, events, _o = open_runtime(repo_root)
    try:
        if opts.subcommand == "list":
            rows = fetch_decisions(conn, limit=opts.limit, actor=opts.actor)
            if opts.work_item:
                rows = [r for r in rows if opts.work_item in (r.get("topic") or "")]
            if effective_json:
                sys.stdout.write(json.dumps({"decisions": rows, "count": len(rows)},
                                            indent=2, sort_keys=True, default=str) + "\n")
            else:
                for r in rows:
                    sys.stdout.write(
                        f"{r['id']}  {r['decided_at']}  {r['actor']:<20} {r['topic']}\n"
                    )
                sys.stdout.write(f"({len(rows)} decision(s))\n")
            return 0
        if opts.subcommand == "show":
            if not opts.decision_id:
                sys.stderr.write("show requires a decision id\n")
                return 64
            match = get_decision(conn, opts.decision_id)
            if match is None:
                sys.stderr.write(f"decision not found: {opts.decision_id}\n")
                return 4
            sys.stdout.write(json.dumps(match, indent=2, sort_keys=True, default=str) + "\n")
            return 0
        if opts.subcommand == "override":
            if not opts.decision_id:
                sys.stderr.write("override requires a decision id\n")
                return 64
            if not opts.severity:
                sys.stderr.write("override requires --severity\n")
                return 64
            match = get_decision(conn, opts.decision_id)
            if match is None:
                sys.stderr.write(f"decision not found: {opts.decision_id}\n")
                return 4
            new_id = record_decision(
                conn,
                phase_id=match["phase_id"],
                topic=match["topic"],
                actor="operator",
                rationale=f"[override severity={opts.severity}] {opts.reason}".strip(),
                consequences=f"overrides decision {opts.decision_id}",
            )
            with transaction(conn):
                conn.execute(
                    "UPDATE decisions SET reversed_by = ? WHERE id = ?;",
                    (new_id, opts.decision_id),
                )
            payload = {"ok": True, "decision_id": new_id, "reversed": opts.decision_id}
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            return 0
        return 64
    finally:
        conn.close()
