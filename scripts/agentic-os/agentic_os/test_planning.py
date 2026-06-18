"""Test plan generator for operator work items.

Consumes the artefacts produced by :mod:`agentic_os.analysis` and renders a
single reviewable Markdown document at
``agentic-os-runtime/plans/<work-item-id>/TEST-PLAN.md``.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from .atomic_io import atomic_write_json, file_lock
from .errors import UsageError
from .events import EventLog
from .paths import RuntimePaths
from .time_utils import now_iso
from .work_items import (
    get_work_item,
    list_work_item_artifacts,
    register_work_item_artifact,
    update_work_item_status,
)


def plan_work_item(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    *,
    work_item_id: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Render TEST-PLAN.md/.json from analysis artefacts.

    ``session_id`` is opt-in (issue #308): when set, ``models.planner``
    is invoked via :func:`agentic_os.models.pipeline.try_invoke_role`
    so the autonomous loop records a ``model_invocations`` row keyed to
    the session. The model output is advisory (written beside the
    canonical TEST-PLAN.md as ``PLANNER-NOTE.md``); the deterministic
    plan remains the source of truth.
    """
    work_item = get_work_item(conn, work_item_id)
    if work_item is None:
        raise UsageError(f"unknown task id: {work_item_id}")
    analysis_dir = paths.runtime_root / "analysis" / work_item_id
    candidates_path = analysis_dir / "candidate-tests.md"
    candidates_json_path = analysis_dir / "candidate-tests.json"
    requirements_path = analysis_dir / "requirements.md"
    sut_map_path = analysis_dir / "sut-map.json"
    if not candidates_path.exists() or not requirements_path.exists():
        raise UsageError(
            f"analysis is missing for {work_item_id}; run `task analyze` first"
        )

    sut_map_text: Optional[str] = None
    if sut_map_path.exists():
        try:
            sut_map_text = json.dumps(
                json.loads(sut_map_path.read_text(encoding="utf-8")),
                indent=2,
                sort_keys=True,
            )
        except json.JSONDecodeError:
            sut_map_text = sut_map_path.read_text(encoding="utf-8")

    plan_dir = paths.runtime_root / "plans" / work_item_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plan_dir / "TEST-PLAN.md"

    artifacts_listed = list_work_item_artifacts(conn, work_item_id)
    analysis_artifacts = [a for a in artifacts_listed if a["kind"] in ("analysis", "sut_map")]

    body = _render_plan(
        work_item=work_item,
        requirements_md=requirements_path.read_text(encoding="utf-8"),
        candidates_md=candidates_path.read_text(encoding="utf-8"),
        sut_map_text=sut_map_text,
        analysis_artifacts=analysis_artifacts,
    )
    plan_path.write_text(body, encoding="utf-8")

    # emit machine-readable TEST-PLAN.json next to the Markdown.
    # Issue #86 — JSON gen failure (or a zero-item plan when analysis
    # had candidates) must NOT be reported as a clean `planned` state.
    # Surface the error and block the work item so downstream
    # `implement-tests` does not silently produce a skeleton-only patch.
    # Issue #273 — advisory cross-run memory. Surface flaky scenarios the
    # system has seen before so downstream scheduling can quarantine them
    # (run them apart from the green path). Pure hint: a read failure must
    # never block planning, hence the broad guard. Emitted into the persisted
    # TEST-PLAN.json summary so the reader (`read_plan_candidates`) sees it too.
    quarantine = _flaky_quarantine(conn, events, work_item_id=work_item_id)

    json_path = plan_dir / "TEST-PLAN.json"
    json_payload: Dict[str, Any]
    plan_error: Optional[str] = None
    plan_items: list = []
    try:
        from .plan_v2 import plan_to_json, summarize_plan

        plan_items = _draft_plan_items_from_analysis(
            sut_map_path=sut_map_path,
            candidates_json_path=candidates_json_path,
        )
        json_payload = plan_to_json(work_item_id, plan_items)
        json_payload["summary"] = summarize_plan(plan_items)
        json_payload["summary"]["quarantine"] = quarantine
        atomic_write_json(json_path, json_payload)
    except Exception as exc:
        plan_error = f"test_plan_json_generation_failed: {exc}"
        json_payload = {
            "version": "1.0",
            "task_id": work_item_id,
            "items": [],
            "summary": {"total": 0, "quarantine": quarantine},
            "error": plan_error,
        }
        atomic_write_json(json_path, json_payload, sort_keys=False, ensure_ascii=True)

    # Zero-item plan despite analysis having candidates → blocked.
    if plan_error is None and not plan_items and _analysis_had_candidates(
        candidates_json_path
    ):
        plan_error = (
            "test_plan_json_has_zero_items_but_analysis_has_candidates"
        )

    next_status = "blocked" if plan_error else "planned"
    next_action = (
        "fix candidate-tests.json shape and rerun `task plan`"
        if plan_error
        else None
    )
    update_work_item_status(conn, events, work_item_id=work_item_id, status=next_status)
    rel = str(plan_path.resolve().relative_to(paths.repo_root.resolve()))

    # Issue #86 — codex review on #128: when planning failed, do NOT
    # register a normal `test_plan` artifact. The downstream suggestions
    # selector treats the presence of a `test_plan` artifact (without a
    # `patch`) as "ready for `implement-tests`", which would let a
    # broken plan reach generation. The plan Markdown remains on disk
    # for the operator, but it is not promoted as a completed artifact.
    artifacts_out: list = []
    if plan_error is None:
        artifact = register_work_item_artifact(
            conn,
            paths,
            events,
            work_item_id=work_item_id,
            kind="test_plan",
            path=rel,
        )
        artifacts_out.append(artifact)
        events.write(
            "work_item.test_plan_drafted",
            actor="operator",
            payload={
                "work_item_id": work_item_id,
                "path": rel,
                "status": next_status,
                "error": None,
            },
        )
    else:
        events.write(
            "work_item.test_plan_blocked",
            severity="warning",
            actor="operator",
            payload={
                "work_item_id": work_item_id,
                "error": plan_error,
                "next_action": next_action,
                "path": rel,
            },
        )

    # Issue #308 — opt-in model invocation. When called from an
    # autonomous session, drive `models.planner` so a row lands in
    # `model_invocations` keyed to session_id (so `budget_status`
    # reflects real cost). Advisory only: the deterministic plan above
    # already shipped; we only attach a side-car PLANNER-NOTE.md when
    # the call succeeds. CLI / dashboard callers pass no session_id and
    # this branch is skipped (no extra row, no extra event).
    if session_id:
        try:
            _attach_planner_note(
                conn,
                paths,
                events,
                work_item_id=work_item_id,
                session_id=session_id,
                plan_dir=plan_dir,
                work_item=work_item,
                requirements_md=requirements_path.read_text(encoding="utf-8"),
                candidates_md=candidates_path.read_text(encoding="utf-8"),
            )
        except Exception:
            # The hook is best-effort: a failure here must not unwind a
            # successfully written plan.
            pass

    return {
        "work_item_id": work_item_id,
        "status": next_status,
        "artifacts": artifacts_out,
        "plan_path": rel,
        "plan_json_path": str(json_path.resolve().relative_to(paths.repo_root.resolve())),
        "plan_summary": json_payload.get("summary", {}),
        "error": plan_error,
        "next_action": next_action,
    }


def _attach_planner_note(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    *,
    work_item_id: str,
    session_id: str,
    plan_dir: Path,
    work_item: Dict[str, Any],
    requirements_md: str,
    candidates_md: str,
) -> None:
    """Issue #308 — drive `models.planner` from inside the planning step.

    Writes the model stdout to ``<plan_dir>/PLANNER-NOTE.md`` when the
    invocation succeeds. Skips silently when no planner role is
    configured / the binary is missing — `try_invoke_role` emits a
    `model.invoke_skipped` event for diagnosable skips.
    """
    from .models.pipeline import try_invoke_role

    prompt = (
        f"# Plan review for {work_item.get('title', work_item_id)}\n\n"
        f"- task_id: {work_item_id}\n"
        f"- spec: {work_item.get('spec_path', '')}\n\n"
        "## Requirements\n\n"
        f"{requirements_md.rstrip()}\n\n"
        "## Candidate tests\n\n"
        f"{candidates_md.rstrip()}\n\n"
        "Suggest coverage gaps, ambiguous assertions, and tests that "
        "should be re-bucketed.\n"
    )
    result = try_invoke_role(
        conn,
        paths,
        events,
        role="planner",
        prompt=prompt,
        work_item_id=work_item_id,
        session_id=session_id,
    )
    if result is None or not result.output_path:
        return
    src = paths.repo_root / result.output_path
    if not src.exists():
        return
    note = plan_dir / "PLANNER-NOTE.md"
    try:
        note.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        pass


def _flaky_quarantine(
    conn: sqlite3.Connection, events: EventLog, *, work_item_id: str
) -> List[str]:
    """Return flaky scenario subjects to quarantine; emit the audit event.

    Advisory only (issue #273): a read failure yields an empty list so
    planning never breaks. Emits `learning.consulted` naming the planner as
    the deciding role when any hint applied, for operator traceability.
    """
    try:
        from .learnings import flaky_subjects

        subjects = flaky_subjects(conn)
    except Exception:
        return []
    if subjects:
        try:
            events.write(
                "learning.consulted",
                actor="planner",
                payload={
                    "kind": "flaky",
                    "work_item_id": work_item_id,
                    "subjects": subjects,
                },
            )
        except Exception:
            pass
    return subjects


def _analysis_had_candidates(candidates_json_path: Path) -> bool:
    """True when `candidate-tests.json` exists and lists at least one item."""
    if not candidates_json_path.exists():
        return False
    try:
        payload = json.loads(candidates_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(payload.get("items"))


def _draft_plan_items_from_analysis(*, sut_map_path: Path, candidates_json_path: Path) -> list:
    """Build PlanItem drafts from structured analysis candidates + OpenAPI.

    All generated items stay in `needs_operator_decision` unless an operator
    later promotes them. This keeps generation auditable while making sure
    analysis candidates do not disappear between Markdown and JSON artefacts.
    """
    from .plan_v2 import PlanItem

    items = _draft_plan_items_from_candidate_json(candidates_json_path)
    seen_ids = {getattr(item, "candidate_id", "") for item in items}

    if not sut_map_path.exists():
        return items
    try:
        sut_map = json.loads(sut_map_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return items
    if not _api_surface_enabled(sut_map):
        return items

    for inv in (sut_map.get("openapi_inventory") or []):
        if "error" in inv:
            continue
        source_path = inv.get("source_path") or "openapi"
        for op in (inv.get("operations") or []):
            method = (op.get("method") or "GET").upper()
            path = op.get("path") or "/"
            op_id = op.get("operation_id") or f"{method.lower()}-{path}"
            candidate_id = _unique_candidate_id(
                f"API-OAS-{op_id.upper().replace('/', '-').strip('-')[:28]}",
                seen_ids,
            )
            status = _best_response_status(op.get("responses") or {})
            expected = (
                f"{method} {path} must return HTTP {status}"
                if status
                else "(operator: write expected behavior with HTTP code and body shape)"
            )
            cleanup = (
                "read-only endpoint"
                if method in {"GET", "HEAD", "OPTIONS"}
                else "(operator: define cleanup for mutating methods)"
            )
            items.append(
                PlanItem(
                    candidate_id=candidate_id,
                    title=op.get("summary") or f"{method} {path}",
                    test_type="api",
                    priority="P2",
                    decision="needs_operator_decision",
                    expected_assertion=expected,
                    source_refs=[f"{source_path}#{path}/{method.lower()}"],
                    target_method=method,
                    target_path=path,
                    required_test_data="(operator: define minimal test data)",
                    cleanup_strategy=cleanup,
                    generator_target="playwright-ts",
                )
            )
    return items


def _api_surface_enabled(sut_map: Dict[str, Any]) -> bool:
    cfg = sut_map.get("config_snapshot") or {}
    api = cfg.get("api")
    return not (isinstance(api, dict) and api.get("enabled") is False)


def _draft_plan_items_from_candidate_json(candidates_json_path: Path) -> list:
    from .plan_v2 import PlanItem

    if not candidates_json_path.exists():
        return []
    try:
        payload = json.loads(candidates_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for raw in payload.get("items") or []:
        if not isinstance(raw, dict):
            continue
        data = {k: v for k, v in raw.items() if k in PlanItem.__dataclass_fields__}
        if not data.get("candidate_id"):
            continue
        data.setdefault("decision", "needs_operator_decision")
        data.setdefault("priority", "P2")
        data.setdefault("expected_assertion", "")
        data.setdefault("source_refs", [])
        data.setdefault("generator_target", "playwright-ts")
        data.setdefault("notes", [])
        try:
            out.append(PlanItem(**data))
        except TypeError:
            continue
    return out


def _best_response_status(responses: Dict[str, Any]) -> Optional[str]:
    if not isinstance(responses, dict):
        return None
    codes = [str(k) for k in responses.keys()]
    for prefix in ("2", "4"):
        exact = sorted(c for c in codes if len(c) == 3 and c.startswith(prefix))
        if exact:
            return exact[0]
    return None


def _unique_candidate_id(base: str, seen: set[str]) -> str:
    candidate = base.strip("-")[:40] or "API-OAS-CASE"
    original = candidate
    counter = 2
    while candidate in seen:
        suffix = f"-{counter}"
        candidate = (original[: 40 - len(suffix)] + suffix).strip("-")
        counter += 1
    seen.add(candidate)
    return candidate


def _render_plan(
    *,
    work_item: Dict[str, Any],
    requirements_md: str,
    candidates_md: str,
    sut_map_text: Optional[str],
    analysis_artifacts: List[Dict[str, Any]],
) -> str:
    lines: List[str] = [
        f"# TEST-PLAN — {work_item['title']}",
        "",
        f"- Task id: `{work_item['id']}`",
        f"- Priority: `{work_item['priority']}`",
        f"- SUT root: `{work_item['sut_root']}`",
        f"- Spec path: `{work_item['spec_path']}`",
        f"- Generated at: `{now_iso()}`",
        "",
        "## Source artefacts",
        "",
    ]
    if analysis_artifacts:
        for art in analysis_artifacts:
            lines.append(f"- `{art['path']}` ({art['kind']})")
    else:
        lines.append("- (none registered yet)")
    lines.extend([
        "",
        "## Requirements (carried over from analysis)",
        "",
        requirements_md.rstrip(),
        "",
        "## Candidate tests (carried over from analysis)",
        "",
        candidates_md.rstrip(),
        "",
        "## SUT map snapshot",
        "",
    ])
    if sut_map_text:
        lines.extend(["```json", sut_map_text, "```"])
    else:
        lines.append("_sut-map.json missing_")
    lines.extend([
        "",
        "## Operator review checklist",
        "",
        "- [ ] Candidates labelled `Needs operator decision` resolved.",
        "- [ ] Bucket priorities confirmed (P0/P1/P2).",
        "- [ ] Out-of-scope items removed before implementation phase.",
        "- [ ] Known bugs reconciled against `Known bugs` section of the task spec.",
        "",
    ])
    return "\n".join(lines).rstrip() + "\n"


def read_plan_candidates(paths: RuntimePaths, *, work_item_id: str) -> Dict[str, Any]:
    json_path = paths.runtime_root / "plans" / work_item_id / "TEST-PLAN.json"
    if not json_path.exists():
        raise UsageError(
            f"TEST-PLAN.json is missing for {work_item_id}; run `task plan` first"
        )
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"cannot read TEST-PLAN.json for {work_item_id}: {exc}") from exc
    return {
        "work_item_id": work_item_id,
        "plan_json_path": str(json_path.resolve().relative_to(paths.repo_root.resolve())),
        "summary": payload.get("summary") or {},
        "items": payload.get("items") or [],
    }


def update_plan_candidate_decision(
    paths: RuntimePaths,
    *,
    work_item_id: str,
    candidate_id: str,
    decision: str,
    expected_assertion: Optional[str] = None,
    required_test_data: Optional[str] = None,
    cleanup_strategy: Optional[str] = None,
    target_page: Optional[str] = None,
    functional_area: Optional[str] = None,
    lifecycle_tags: Optional[list] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    from .plan_v2 import PlanItem, plan_to_json, summarize_plan, validate_plan

    json_path = paths.runtime_root / "plans" / work_item_id / "TEST-PLAN.json"
    # Issue #161 — the whole read-modify-write must run under a single
    # exclusive lock. Without it two concurrent approvals would both
    # read the pre-write payload and the second os.replace would clobber
    # the first writer's decision (last-write-wins). The lock is keyed
    # on the per-task TEST-PLAN.json path so concurrent approvals on
    # different tasks still run in parallel.
    with file_lock(json_path):
        return _update_plan_candidate_decision_locked(
            paths,
            work_item_id=work_item_id,
            candidate_id=candidate_id,
            decision=decision,
            expected_assertion=expected_assertion,
            required_test_data=required_test_data,
            cleanup_strategy=cleanup_strategy,
            target_page=target_page,
            functional_area=functional_area,
            lifecycle_tags=lifecycle_tags,
            reason=reason,
        )


def approve_all_runnable_candidates(
    paths: RuntimePaths,
    *,
    work_item_id: str,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Bulk-flip every runnable candidate to ``decision='generate_now'``.

    Wave 13 (#313 / RC gap 2) — the dashboard has shipped a one-click
    bulk-approve since #270/#247 but the CLI only exposed per-candidate
    approval, forcing scripted operators into N round-trips. This helper
    is the shared core: it returns the same outcome shape as the
    dashboard endpoint (``approved``, ``skipped``, ``failed``,
    per-candidate ``outcomes``) so the CLI and HTTP surfaces stay in
    lock-step and a future change applies to both.

    Unsupported, already-approved, or operator-rejected candidates are
    skipped (with the reason recorded), keeping the call idempotent.
    """
    payload = read_plan_candidates(paths, work_item_id=work_item_id)
    outcomes: List[Dict[str, Any]] = []
    approved = 0
    skipped = 0
    failed = 0
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        candidate_id = item.get("candidate_id")
        if not candidate_id:
            continue
        if item.get("decision") == "generate_now":
            skipped += 1
            outcomes.append(
                {
                    "candidate_id": candidate_id,
                    "status": "skipped",
                    "reason": "already generate_now",
                }
            )
            continue
        if item.get("decision") == "not_testable":
            skipped += 1
            outcomes.append(
                {
                    "candidate_id": candidate_id,
                    "status": "skipped",
                    "reason": "not_testable",
                }
            )
            continue
        if item.get("test_type") not in {"api", "ui"}:
            skipped += 1
            outcomes.append(
                {
                    "candidate_id": candidate_id,
                    "status": "skipped",
                    "reason": f"unsupported test_type={item.get('test_type')}",
                }
            )
            continue
        try:
            update_plan_candidate_decision(
                paths,
                work_item_id=work_item_id,
                candidate_id=candidate_id,
                decision="generate_now",
                expected_assertion=item.get("expected_assertion"),
                required_test_data=item.get("required_test_data") or item.get("test_data"),
                cleanup_strategy=item.get("cleanup_strategy"),
                target_page=item.get("target_page"),
                functional_area=item.get("functional_area") or None,
                lifecycle_tags=item.get("lifecycle_tags") or None,
                reason=reason or "bulk approve-all",
            )
            approved += 1
            outcomes.append({"candidate_id": candidate_id, "status": "approved"})
        except UsageError as exc:
            failed += 1
            outcomes.append(
                {
                    "candidate_id": candidate_id,
                    "status": "failed",
                    "reason": str(exc),
                }
            )
    final = read_plan_candidates(paths, work_item_id=work_item_id)
    return {
        "work_item_id": work_item_id,
        "approved": approved,
        "skipped": skipped,
        "failed": failed,
        "outcomes": outcomes,
        "summary": final.get("summary") or {},
    }


def _update_plan_candidate_decision_locked(
    paths: RuntimePaths,
    *,
    work_item_id: str,
    candidate_id: str,
    decision: str,
    expected_assertion: Optional[str],
    required_test_data: Optional[str],
    cleanup_strategy: Optional[str],
    target_page: Optional[str],
    functional_area: Optional[str],
    lifecycle_tags: Optional[list],
    reason: Optional[str],
) -> Dict[str, Any]:
    from .plan_v2 import PlanItem, plan_to_json, summarize_plan, validate_plan

    current = read_plan_candidates(paths, work_item_id=work_item_id)
    items_raw = list(current["items"])
    updated_items = []
    found = False
    for raw in items_raw:
        if not isinstance(raw, dict):
            continue
        next_raw = dict(raw)
        if next_raw.get("candidate_id") == candidate_id:
            found = True
            next_raw["decision"] = decision
            if expected_assertion is not None:
                next_raw["expected_assertion"] = expected_assertion
            if required_test_data is not None:
                next_raw["required_test_data"] = required_test_data
            if cleanup_strategy is not None:
                next_raw["cleanup_strategy"] = cleanup_strategy
            if target_page is not None:
                next_raw["target_page"] = target_page
            if functional_area is not None:
                next_raw["functional_area"] = functional_area
            elif decision == "generate_now" and not next_raw.get("functional_area"):
                # Issue #105 — fall back to a sensible default so the
                # plan validator's mandatory tag does not block legacy
                # CLI flows that have not yet been updated.
                next_raw["functional_area"] = "functional-general"
            if lifecycle_tags is not None:
                next_raw["lifecycle_tags"] = list(lifecycle_tags)
            elif decision == "generate_now" and not next_raw.get("lifecycle_tags"):
                next_raw["lifecycle_tags"] = ["regression"]
            notes = list(next_raw.get("notes") or [])
            if reason:
                notes.append(f"operator_reason: {reason}")
            next_raw["notes"] = notes
        data = {k: v for k, v in next_raw.items() if k in PlanItem.__dataclass_fields__}
        try:
            updated_items.append(PlanItem(**data))
        except TypeError as exc:
            raise UsageError(
                f"candidate {next_raw.get('candidate_id') or '(unknown)'} is invalid: {exc}"
            ) from exc
    if not found:
        raise UsageError(f"candidate not found in TEST-PLAN.json: {candidate_id}")

    findings = validate_plan(updated_items)
    blocking = [f for f in findings if f.severity == "P0"]
    if decision == "generate_now" and blocking:
        details = "; ".join(
            f"{f.candidate_id}: {f.message}" for f in blocking if f.candidate_id == candidate_id
        ) or "; ".join(f"{f.candidate_id}: {f.message}" for f in blocking)
        raise UsageError(f"cannot approve candidate; plan validation failed: {details}")

    json_path = paths.runtime_root / "plans" / work_item_id / "TEST-PLAN.json"
    payload = plan_to_json(work_item_id, updated_items)
    payload["summary"] = summarize_plan(updated_items)
    payload["findings"] = [
        {"candidate_id": f.candidate_id, "severity": f.severity, "message": f.message}
        for f in findings
    ]
    atomic_write_json(json_path, payload)
    return {
        "work_item_id": work_item_id,
        "candidate_id": candidate_id,
        "decision": decision,
        "plan_json_path": str(json_path.resolve().relative_to(paths.repo_root.resolve())),
        "summary": payload["summary"],
        "findings": payload["findings"],
    }
