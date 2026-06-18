"""Report triage + flaky detection (issue #292)."""
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
from .evidence import _persist_test_results_and_evidence, _scenario_tag, _triage_evidence



def _flaky_subject(raw: Dict[str, Any], classification: Any) -> str:
    """Stable `feature_uri::scenario` key for a failure (issue #287)."""
    feature = str(raw.get("feature_uri") or raw.get("classname") or classification.suite or "")
    scenario = str(raw.get("scenario") or classification.name or "")
    return f"{feature}::{scenario}"

def _detect_flaky_oscillation(
    events: EventLog,
    classifications: List[Any],
    failures_by_name: Dict[str, Any],
    *,
    run_id_str: str,
) -> None:
    """Record `flaky` learnings for scenarios whose category oscillates.

    For each currently-failing scenario, emit a `triage.scenario_classified`
    event (subject + category in the payload, mirroring Part A's scope-in-
    payload pattern). When the current category is non-product AND differs
    from the most-recent prior classification for the same subject, that is
    an alternation with no steady product bug → record a `flaky` learning.
    Wholly best-effort: a failure here never affects triage output.
    """
    try:
        conn = events._conn  # reuse the runtime connection; pure best-effort
    except Exception:
        return
    for classification in classifications:
        try:
            raw = failures_by_name.get(classification.name, {})
            subject = _flaky_subject(raw, classification)
            category = classification.category
            # Most-recent prior classification for this subject (exclude the
            # event we are about to write).
            prior_row = conn.execute(
                "SELECT payload FROM events WHERE kind='triage.scenario_classified' "
                "AND json_extract(payload, '$.subject') = ? "
                "ORDER BY ts DESC, id DESC LIMIT 1;",
                (subject,),
            ).fetchone()
            prior_category = None
            if prior_row is not None:
                payload_raw = prior_row["payload"] if hasattr(prior_row, "keys") else prior_row[0]
                try:
                    prior_payload = json.loads(payload_raw) if isinstance(payload_raw, str) else {}
                    prior_category = prior_payload.get("category")
                except (ValueError, TypeError):
                    prior_category = None
            # Record the current classification for future-run detection.
            events.write(
                "triage.scenario_classified",
                run_id=run_id_str,
                payload={"subject": subject, "category": category},
            )
            # Oscillation: current is non-product and differs from the prior.
            if (
                prior_category is not None
                and category != "product_bug"
                and prior_category != category
            ):
                from ...learnings import record_learning

                record_learning(
                    conn,
                    kind="flaky",
                    subject=subject,
                    payload={
                        "category": category,
                        "prior_category": prior_category,
                        "reason": "category oscillation across triage runs",
                    },
                    actor="triager",
                )
        except Exception:
            # One scenario's detection failure must not abort the others.
            continue

def triage_reports(
    paths: RuntimePaths,
    events: EventLog,
    *,
    run_id_str: str,
    auto_file_bugs: bool,
) -> Dict[str, Any]:
    """Classify last-run failures and optionally file product bugs.

    The shell report scripts remain the source of raw run data. This function
    turns `reports/last-run.json` into durable triage artifacts consumed by the
    dashboard and final-gate evidence review.
    """
    from ... import qualitycat
    from ...results import TestResult, classify_results, summarize_classifications

    last_run_path = paths.repo_root / "reports" / "last-run.json"
    if not last_run_path.exists():
        return {"available": False, "reason": "missing reports/last-run.json", "bugs_opened": []}
    try:
        last_run = json.loads(last_run_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"available": False, "reason": f"invalid reports/last-run.json: {exc}", "bugs_opened": []}

    failures = list(last_run.get("failures") or [])
    claimed_known_bug_ids = {
        tag
        for failure in failures
        if "@known-bug" in (failure.get("tags") or [])
        for tag in (failure.get("tags") or [])
        if isinstance(tag, str) and re.match(r"@bug-\d+", tag)
    }
    # Issue #110 — a `@known-bug @bug-NNN` pair is only credible when the
    # referenced bug actually exists in `bugs/` or the SQLite registry.
    # Self-declared tags without a record are treated as product failures
    # and a policy-violation event is emitted so the dashboard can surface
    # the false-known-bug attempt.
    resolved_known, unresolved_known = _resolve_known_bug_tags(
        paths, claimed_known_bug_ids
    )
    for unresolved_tag in sorted(unresolved_known):
        events.write(
            "triage.known_bug_unresolved",
            severity="warning",
            payload={
                "tag": unresolved_tag,
                "reason": "no_bug_record",
                "run_id": run_id_str,
            },
        )
    known_bug_ids = resolved_known
    test_results = [
        TestResult(
            name=str(failure.get("scenario") or failure.get("name") or "(unnamed failure)"),
            suite=str(failure.get("classname") or failure.get("feature_uri") or "last-run"),
            status="failed",
            failure_message=str(
                failure.get("error_message")
                or failure.get("stack_head")
                or failure.get("error_type")
                or ""
            ),
            tags=[str(t) for t in (failure.get("tags") or [])],
            runner="last-run",
        )
        for failure in failures
    ]
    classifications = classify_results(test_results, known_bug_ids=known_bug_ids)
    failures_by_name = {tr.name: raw for tr, raw in zip(test_results, failures)}
    bugs_opened: List[str] = []
    triage_items: List[Dict[str, Any]] = []

    # Issue #287 — flaky producer. Emit a per-scenario classification event so
    # a future run can detect category oscillation, and record a `flaky`
    # learning when a non-product failure for a subject differs from its most
    # recent prior classification (alternation, no steady product bug).
    # Best-effort and advisory; never affects classification or bug filing.
    _detect_flaky_oscillation(
        events, classifications, failures_by_name, run_id_str=run_id_str
    )

    conn = None
    if auto_file_bugs and any(c.category == "product_bug" for c in classifications):
        conn = _db_connect(paths.db)
    try:
        for classification in classifications:
            raw = failures_by_name.get(classification.name, {})
            tags_list = [str(t) for t in (raw.get("tags") or [])]
            claimed_known = "@known-bug" in tags_list
            referenced_bug_tags = [
                t for t in tags_list if isinstance(t, str) and re.match(r"@bug-\d+", t)
            ]
            unresolved_claims = [
                t for t in referenced_bug_tags if t in unresolved_known
            ]
            policy_violation = bool(
                claimed_known and referenced_bug_tags and unresolved_claims
            )
            item: Dict[str, Any] = {
                "name": classification.name,
                "suite": classification.suite,
                "category": classification.category,
                "reason": classification.reason,
                "bug_id": classification.bug_id,
                "tags": tags_list,
                "evidence": _triage_evidence(paths, raw),
                "known_bug_policy_violation": policy_violation,
                "unresolved_known_bug_tags": unresolved_claims,
            }
            if classification.category == "product_bug" and auto_file_bugs and conn is not None:
                title = f"Exact-spec failure: {classification.name}"
                scenario_tag = _scenario_tag(raw)
                evidence_files = [
                    paths.repo_root / rel
                    for rel in item["evidence"]
                    if rel and (paths.repo_root / rel).exists()
                ]
                # Issue #85 — pass full triage data so the bug Markdown
                # is hydrated, not a TBD skeleton.
                test_id = (
                    str(raw.get("classname") or "")
                    or str(raw.get("feature_uri") or "")
                    or classification.name
                )
                error_message_full = (
                    str(raw.get("error_message") or "")
                    or str(raw.get("error_type") or "")
                ) or None
                actual_body = str(raw.get("stack_head") or raw.get("error_message") or "") or None
                spec_source = str(raw.get("feature_uri") or "") or None
                tags_for_repro = [
                    t for t in (raw.get("tags") or []) if isinstance(t, str) and t.startswith("@")
                ]
                # Only build `--tags <tag>` from a real Cucumber tag.
                # `_scenario_tag()` falls back to a scenario slug like
                # `untagged-scenario` when no `@` tag exists; passing that
                # as `--tags` produces a misleading repro command (codex
                # review on #127, issue #85).
                primary_tag = tags_for_repro[0] if tags_for_repro else None
                repro_cmd = (
                    f"./run-tests.sh --tags {primary_tag}"
                    if primary_tag
                    else "./run-tests.sh"
                )
                try:
                    filed = qualitycat.file_bug(
                        paths=paths,
                        events=events,
                        conn=conn,
                        title=title,
                        severity="P1",
                        scenario_tag=scenario_tag,
                        evidence_files=evidence_files,
                        test_id=test_id or None,
                        error_message=error_message_full,
                        actual=actual_body,
                        repro_command=repro_cmd,
                        spec_source=spec_source,
                    )
                    item["filed_bug_id"] = filed.bug_id
                    item["bug_path"] = str(filed.bug_md_path.relative_to(paths.repo_root))
                    bugs_opened.append(filed.bug_id)
                except Exception as exc:
                    item["bug_file_error"] = str(exc)
                    events.write(
                        "bug.auto_file_failed",
                        severity="warning",
                        run_id=run_id_str,
                        payload={"scenario": classification.name, "error": str(exc)},
                    )
            triage_items.append(item)
    finally:
        if conn is not None:
            conn.close()

    triage_dir = paths.runtime_root / "runs" / run_id_str
    triage_dir.mkdir(parents=True, exist_ok=True)
    # Issue #75 — the triage summary previously reported failure-only
    # categories, so a green run with 267 passing tests showed
    # `total=0, pass=0`. Carry totals from `reports/last-run.json` into
    # the summary so dashboard/API consumers can distinguish
    # `no tests collected` from `everything passed`.
    summary_block = summarize_classifications(classifications)
    run_totals = {
        "run_total": int(last_run.get("total") or 0),
        "run_passed": int(last_run.get("passed") or 0),
        "run_failed": int(last_run.get("failed") or 0),
        "run_skipped": int(last_run.get("skipped") or 0),
        "failure_total": sum(
            1 for c in classifications
            if c.category in {"product_bug", "known_bug_red", "test_bug", "infra"}
        ),
    }
    summary_block.update(run_totals)
    # Backwards-compat: the `pass` category previously read 0 on green
    # runs; now it reflects the actual passing test count when there are
    # no failure classifications (otherwise the per-classification
    # `pass` count from `summarize_classifications` stays authoritative).
    if summary_block.get("pass", 0) == 0 and run_totals["run_passed"] > 0:
        summary_block["pass"] = run_totals["run_passed"]
        summary_block["total"] = run_totals["run_total"]

    # Issue #103 — persist a `test_results` row per parsed failure and
    # an `evidence` row per linked artifact so dashboard, status, and
    # final-gate logic can rely on SQLite as the durable source of
    # truth instead of grepping JSON/Markdown.
    try:
        _persist_test_results_and_evidence(
            paths=paths,
            events=events,
            run_id=run_id_str,
            failures=failures,
            triage_items=triage_items,
        )
    except Exception as exc:
        events.write(
            "reports.persistence_failed",
            severity="warning",
            payload={"error": str(exc)},
        )

    payload = {
        "available": True,
        "run_id": run_id_str,
        "source": "reports/last-run.json",
        "summary": summary_block,
        "bugs_opened": bugs_opened,
        "items": triage_items,
    }
    json_path = triage_dir / "triage.json"
    md_path = triage_dir / "triage.md"
    atomic_write_json(json_path, payload)
    md_path.write_text(_render_triage_markdown(payload), encoding="utf-8")
    events.write(
        "reports.triaged",
        run_id=run_id_str,
        payload={
            "triage_json": str(json_path.relative_to(paths.repo_root)),
            "triage_md": str(md_path.relative_to(paths.repo_root)),
            "summary": payload["summary"],
            "bugs_opened": bugs_opened,
        },
    )
    payload["triage_json"] = str(json_path.relative_to(paths.repo_root))
    payload["triage_md"] = str(md_path.relative_to(paths.repo_root))
    return payload

_BUG_TAG_ID_RE = re.compile(r"@bug-(\d+)", re.IGNORECASE)

def _resolve_known_bug_tags(
    paths: RuntimePaths,
    claimed: set[str],
) -> tuple[set[str], set[str]]:
    """Partition `@bug-NNN` tags into (resolved, unresolved) — issue #110.

    A tag is resolved when either:
    - `bugs/BUG-NNN-*.md` exists in the repo, or
    - SQLite `bugs` table has a row whose id is `BUG-NNN`.
    """
    resolved: set[str] = set()
    unresolved: set[str] = set()
    bugs_dir = paths.repo_root / "bugs"
    db_ids: set[str] = set()
    if paths.db.exists():
        try:
            db = _db_connect(paths.db)
            try:
                for row in db.execute("SELECT id FROM bugs;").fetchall():
                    val = row["id"] if isinstance(row, sqlite3.Row) else row[0]
                    db_ids.add(str(val).upper())
            finally:
                db.close()
        except sqlite3.Error:
            # Triage must never hide failures because of DB IO issues.
            db_ids = set()
    for tag in claimed:
        match = _BUG_TAG_ID_RE.match(tag)
        if match is None:
            unresolved.add(tag)
            continue
        bug_num = match.group(1)
        bug_id = f"BUG-{bug_num}"
        has_file = (
            bugs_dir.is_dir()
            and bool(list(bugs_dir.glob(f"BUG-{bug_num}-*.md")))
        )
        if has_file or bug_id.upper() in db_ids:
            resolved.add(tag)
        else:
            unresolved.add(tag)
    return resolved, unresolved

def _render_triage_markdown(payload: Dict[str, Any]) -> str:
    lines = [
        f"# Run triage - {payload.get('run_id')}",
        "",
        f"- Source: `{payload.get('source')}`",
        f"- Bugs opened: `{len(payload.get('bugs_opened') or [])}`",
        "",
        "## Summary",
        "",
        "| Category | Count |",
        "|---|---:|",
    ]
    for key, value in sorted((payload.get("summary") or {}).items()):
        lines.append(f"| `{key}` | {value} |")
    lines.extend(["", "## Failures", ""])
    items = payload.get("items") or []
    if not items:
        lines.append("_No failures to triage._")
    for item in items:
        lines.extend(
            [
                f"### {item.get('name')}",
                "",
                f"- Category: `{item.get('category')}`",
                f"- Reason: {item.get('reason')}",
                f"- Bug: `{item.get('filed_bug_id') or item.get('bug_id') or 'none'}`",
                "- Evidence:",
            ]
        )
        for evidence in item.get("evidence") or []:
            lines.append(f"  - `{evidence}`")
        if item.get("bug_file_error"):
            lines.append(f"- Bug file error: `{item['bug_file_error']}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
