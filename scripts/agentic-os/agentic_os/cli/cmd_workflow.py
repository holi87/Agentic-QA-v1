"""Workflow commands: task (issue #292)."""

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
from .cmd_docs import _validated_config_sut_root


def cmd_task(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    sub = argparse.ArgumentParser(prog="agentic-os task", add_help=True)
    sub.add_argument(
        "action",
        choices=(
            "create",
            "list",
            "show",
            "analyze",
            "plan",
            "candidates",
            "approve-candidate",
            "approve-all-candidates",
            "reject-candidate",
            "mark-needs-decision",
            "implement-tests",
            "abandon-patch",
            "prune-orphans",
            "link",
        ),
    )
    sub.add_argument("value", nargs="?")
    sub.add_argument("candidate_id", nargs="?")
    sub.add_argument("--patch", dest="patch", default=None)
    sub.add_argument("--reason", dest="reason", default=None)
    # Issue #274 — `task link <child> --blocks <parent>` (or `--needs`).
    # Parent must finish (status `done`) before the child becomes selectable
    # under the DEPENDENCY/HYBRID queue policies. `--blocks` and `--needs` are
    # synonyms (both name the prerequisite parent).
    sub.add_argument("--blocks", dest="blocks", default=None)
    sub.add_argument("--needs", dest="needs", default=None)
    sub.add_argument("--expected-assertion", dest="expected_assertion", default=None)
    sub.add_argument("--test-data", dest="test_data", default=None)
    sub.add_argument("--cleanup-strategy", dest="cleanup_strategy", default=None)
    sub.add_argument("--target-page", dest="target_page", default=None)
    # Issue #288 — scope a created work item to a project. Resolution order:
    # this flag > config `project.active` > the always-present `default`.
    sub.add_argument("--project", dest="project", default=None)
    opts = sub.parse_args(args)

    conn, paths, events, _orch = open_runtime(repo_root)
    try:
        if opts.action == "create":
            if not opts.value:
                raise UsageError("task create requires a Markdown spec file")
            default_sut_root = _validated_config_sut_root(repo_root)
            from ..config import load_or_default
            from ..projects import resolve_active_project_id

            project_id = resolve_active_project_id(
                conn,
                load_or_default(repo_root, override=config_override),
                explicit=opts.project,
            )
            detail = create_work_item_from_file(
                conn,
                paths,
                events,
                Path(opts.value),
                default_sut_root=default_sut_root,
                project_id=project_id,
            )
            if json_output:
                sys.stdout.write(json.dumps(detail, indent=2, sort_keys=True) + "\n")
            else:
                item = detail["work_item"]
                sys.stdout.write(
                    "task created\n"
                    f"  id:       {item['id']}\n"
                    f"  status:   {item['status']}\n"
                    f"  priority: {item['priority']}\n"
                    f"  spec:     {item['spec_path']}\n"
                )
            return 0

        if opts.action == "list":
            items = annotate_spec_status(paths, list_work_items(conn))
            if json_output:
                sys.stdout.write(json.dumps({"tasks": items}, indent=2, sort_keys=True) + "\n")
            elif not items:
                sys.stdout.write("(no tasks)\n")
            else:
                sys.stdout.write("id\tstatus\tpriority\ttitle\tspec\tspec_missing\n")
                for item in items:
                    sys.stdout.write(
                        f"{item['id']}\t{item['status']}\t{item['priority']}\t"
                        f"{item['title']}\t{item['spec_path']}\t"
                        f"{'MISSING' if item.get('spec_missing') else 'ok'}\n"
                    )
                missing = sum(1 for i in items if i.get("spec_missing"))
                if missing:
                    sys.stdout.write(
                        f"\n{missing} task(s) have a missing spec file. "
                        "Run `agentic-os task prune-orphans` to drop the orphan rows.\n"
                    )
            return 0

        if opts.action == "prune-orphans":
            pruned = prune_orphan_work_items(conn, paths, events)
            if json_output:
                sys.stdout.write(json.dumps({"pruned": pruned}, indent=2, sort_keys=True) + "\n")
            elif not pruned:
                sys.stdout.write("(no orphan tasks to prune)\n")
            else:
                sys.stdout.write(f"pruned {len(pruned)} orphan task(s):\n")
                for row in pruned:
                    sys.stdout.write(
                        f"  - {row['id']} ({row['title']}) — {row['spec_path']}\n"
                    )
            return 0

        if opts.action == "link":
            if not opts.value:
                raise UsageError("task link requires a <child> task id")
            parent_id = opts.blocks or opts.needs
            if not parent_id:
                raise UsageError(
                    "task link requires --blocks <parent> (or --needs <parent>) "
                    "naming the prerequisite task"
                )
            edge = link_work_items(
                conn,
                events,
                parent_id=parent_id,
                child_id=opts.value,
            )
            if json_output:
                sys.stdout.write(json.dumps(edge, indent=2, sort_keys=True) + "\n")
            else:
                sys.stdout.write(
                    "dependency linked\n"
                    f"  child:  {edge['child_id']}\n"
                    f"  needs:  {edge['parent_id']} (must reach 'done' first)\n"
                )
            return 0

        if opts.action == "show":
            if not opts.value:
                raise UsageError("task show requires a task id")
            detail = get_work_item_detail(conn, opts.value)
            if detail is None:
                raise UsageError(f"unknown task id: {opts.value}")
            if json_output:
                sys.stdout.write(json.dumps(detail, indent=2, sort_keys=True) + "\n")
            else:
                item = detail["work_item"]
                sys.stdout.write(
                    f"id:       {item['id']}\n"
                    f"title:    {item['title']}\n"
                    f"status:   {item['status']}\n"
                    f"priority: {item['priority']}\n"
                    f"sut_root: {item['sut_root']}\n"
                    f"spec:     {item['spec_path']}\n"
                    "artifacts:\n"
                )
                for artifact in detail["artifacts"]:
                    sys.stdout.write(
                        f"  - {artifact['kind']}: {artifact['path']} ({artifact['id']})\n"
                    )
            return 0

        if opts.action == "analyze":
            if not opts.value:
                raise UsageError("task analyze requires a task id")
            result = analyze_work_item(conn, paths, events, work_item_id=opts.value)
            if json_output:
                sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
            else:
                sys.stdout.write(
                    f"task analyzed: {opts.value}\n"
                    f"  status:   {result['status']}\n"
                    "  artifacts:\n"
                )
                for artifact in result["artifacts"]:
                    sys.stdout.write(
                        f"    - {artifact['kind']}: {artifact['path']}\n"
                    )
                if result.get("config_warning"):
                    sys.stdout.write(f"  note: {result['config_warning']}\n")
            return 0

        if opts.action == "plan":
            if not opts.value:
                raise UsageError("task plan requires a task id")
            result = plan_work_item(conn, paths, events, work_item_id=opts.value)
            if json_output:
                sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
            else:
                sys.stdout.write(
                    f"task planned: {opts.value}\n"
                    f"  status: {result['status']}\n"
                    f"  plan:   {result['plan_path']}\n"
                )
            return 0

        if opts.action == "candidates":
            if not opts.value:
                raise UsageError("task candidates requires a task id")
            result = read_plan_candidates(paths, work_item_id=opts.value)
            if json_output:
                sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
            else:
                sys.stdout.write(
                    f"candidates for {opts.value}\n"
                    f"  plan: {result['plan_json_path']}\n"
                    f"  summary: {result['summary']}\n"
                )
                for item in result["items"]:
                    sys.stdout.write(
                        f"  - {item.get('candidate_id')}: "
                        f"{item.get('decision')} "
                        f"{item.get('test_type')} "
                        f"{item.get('title')}\n"
                    )
            return 0

        if opts.action == "approve-all-candidates":
            # Wave 13 (#313 / RC gap 2) — symmetry with the dashboard's
            # one-click bulk approve, so scripted operators don't fan out
            # per-candidate calls. Shares the same outcome shape as the
            # HTTP endpoint via `approve_all_runnable_candidates`.
            if not opts.value:
                raise UsageError("task approve-all-candidates requires a task id")
            result = approve_all_runnable_candidates(
                paths,
                work_item_id=opts.value,
                reason=opts.reason or "CLI approve-all-candidates",
            )
            if json_output:
                sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
            else:
                sys.stdout.write(
                    f"approve-all-candidates: {opts.value}\n"
                    f"  approved: {result['approved']}\n"
                    f"  skipped:  {result['skipped']}\n"
                    f"  failed:   {result['failed']}\n"
                )
                for o in result.get("outcomes", []):
                    sys.stdout.write(
                        f"  - {o.get('candidate_id')}: {o.get('status')}"
                        + (f" — {o.get('reason')}" if o.get("reason") else "")
                        + "\n"
                    )
            return 0

        if opts.action in {"approve-candidate", "reject-candidate", "mark-needs-decision"}:
            if not opts.value:
                raise UsageError(f"task {opts.action} requires a task id")
            if not opts.candidate_id:
                raise UsageError(f"task {opts.action} requires a candidate id")
            decision = {
                "approve-candidate": "generate_now",
                "reject-candidate": "not_testable",
                "mark-needs-decision": "needs_operator_decision",
            }[opts.action]
            result = update_plan_candidate_decision(
                paths,
                work_item_id=opts.value,
                candidate_id=opts.candidate_id,
                decision=decision,
                expected_assertion=opts.expected_assertion,
                required_test_data=opts.test_data,
                cleanup_strategy=opts.cleanup_strategy,
                target_page=opts.target_page,
                reason=opts.reason,
            )
            if json_output:
                sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
            else:
                sys.stdout.write(
                    f"candidate updated: {opts.candidate_id}\n"
                    f"  decision: {decision}\n"
                    f"  plan:     {result['plan_json_path']}\n"
                    f"  summary:  {result['summary']}\n"
                )
            return 0

        if opts.action == "implement-tests":
            if not opts.value:
                raise UsageError("task implement-tests requires a task id")
            result = implement_tests_for_work_item(
                conn, paths, events, work_item_id=opts.value
            )
            if json_output:
                sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
            else:
                sys.stdout.write(
                    f"task implement-tests: {opts.value}\n"
                    f"  status:     {result['status']}\n"
                    f"  patch:      {result['patch_path']}\n"
                    f"  target:     {result['target_path']}\n"
                )
            return 0

        if opts.action == "abandon-patch":
            if not opts.value:
                raise UsageError("task abandon-patch requires a task id")
            if not opts.patch:
                raise UsageError("task abandon-patch requires --patch <path>")
            if not opts.reason:
                raise UsageError("task abandon-patch requires --reason <text>")
            from ..workflows import abandon_patch

            result = abandon_patch(
                paths,
                events,
                task_id=opts.value,
                patch_path=opts.patch,
                reason=opts.reason,
            )
            if json_output:
                sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
            else:
                sys.stdout.write(
                    f"patch abandoned for task {opts.value}\n"
                    f"  patch:    {result['patch_path']}\n"
                    f"  decision: {result['decision_id']}\n"
                    f"  artifact: {result['artifact_path']}\n"
                )
            return 0
    finally:
        conn.close()

    raise UsageError(f"unknown task action: {opts.action}")
