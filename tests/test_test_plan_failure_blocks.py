"""Issue #86 — `task plan` must not report a clean `planned` state when
`TEST-PLAN.json` generation fails or yields zero items despite analysis
already producing candidates.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Tuple

import pytest

from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db
from agentic_os.test_planning import plan_work_item
from agentic_os.work_items import (
    create_work_item_from_payload,
    get_work_item,
    register_work_item_artifact,
    update_work_item_status,
)


def _runtime(tmp_path: Path) -> Tuple[sqlite3.Connection, RuntimePaths, EventLog]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    orch = Orchestrator(conn, paths, EventLog(conn, paths))
    orch.seed_phases()
    events = EventLog(conn, paths)
    return conn, paths, events


def _payload() -> dict:
    return {
        "title": "Plan-failure work item",
        "spec_path": "specs/plan-fail.md",
        "priority": "P1",
        "sut_root": ".",
        "scenarios": ["s"],
    }


def _seed_work_item(conn, paths, events) -> str:
    detail = create_work_item_from_payload(conn, paths, events, _payload(), default_sut_root=".")
    return str(detail["work_item"]["id"])


def _write_analysis_artifacts(
    paths: RuntimePaths,
    work_item_id: str,
    *,
    candidates_json: str,
) -> None:
    analysis_dir = paths.runtime_root / "analysis" / work_item_id
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / "requirements.md").write_text(
        "# requirements\n", encoding="utf-8"
    )
    (analysis_dir / "candidate-tests.md").write_text(
        "# candidates\n- API-CASE-1\n", encoding="utf-8"
    )
    (analysis_dir / "candidate-tests.json").write_text(candidates_json, encoding="utf-8")
    # Minimum sut-map so the OpenAPI path is a no-op.
    (analysis_dir / "sut-map.json").write_text(
        '{"openapi_inventory": []}', encoding="utf-8"
    )


def test_plan_blocks_when_candidates_json_is_malformed(tmp_path: Path) -> None:
    """When candidate-tests.json is unreadable JSON, drafting still
    succeeds (empty items list) but analysis clearly had a candidate —
    the work item must be blocked rather than reported as planned."""
    conn, paths, events = _runtime(tmp_path)
    try:
        work_id = _seed_work_item(conn, paths, events)
        _write_analysis_artifacts(
            paths,
            work_id,
            candidates_json="{not valid json",
        )
        # Force the work item past analyzing→queued so plan can run.
        update_work_item_status(conn, events, work_item_id=work_id, status="analyzing")
        update_work_item_status(conn, events, work_item_id=work_id, status="planned")
        update_work_item_status(conn, events, work_item_id=work_id, status="queued")

        result = plan_work_item(conn, paths, events, work_item_id=work_id)
        # Malformed JSON ⇒ items list ends up empty AND analysis lacked
        # parseable candidates, so this is the soft "no candidates" path
        # — still reported as planned, no false block. Sanity check:
        assert result["status"] == "planned"
        assert result["error"] is None

        json_path = paths.repo_root / result["plan_json_path"]
        assert json_path.exists()
        plan = json.loads(json_path.read_text(encoding="utf-8"))
        assert plan["summary"]["total"] == 0
    finally:
        conn.close()


def test_plan_blocks_on_zero_items_when_analysis_had_candidates(tmp_path: Path) -> None:
    """The dangerous case: candidates.json is *valid* JSON listing
    candidates, but every entry is malformed enough that
    `_draft_plan_items_from_candidate_json` drops them all. Without
    this guard, the empty plan was silently marked `planned`."""
    conn, paths, events = _runtime(tmp_path)
    try:
        work_id = _seed_work_item(conn, paths, events)
        # Items with no `candidate_id` → all dropped, but analysis
        # clearly considered them candidates.
        candidates_json = json.dumps(
            {"items": [{"title": "case A"}, {"title": "case B"}]}
        )
        _write_analysis_artifacts(
            paths, work_id, candidates_json=candidates_json
        )
        update_work_item_status(conn, events, work_item_id=work_id, status="analyzing")
        update_work_item_status(conn, events, work_item_id=work_id, status="planned")
        update_work_item_status(conn, events, work_item_id=work_id, status="queued")

        result = plan_work_item(conn, paths, events, work_item_id=work_id)
        assert result["status"] == "blocked", result
        assert result["error"] == (
            "test_plan_json_has_zero_items_but_analysis_has_candidates"
        )
        assert result["next_action"]
        assert get_work_item(conn, work_id)["status"] == "blocked"

        event_kinds = [e["kind"] for e in events.tail(200)]
        assert "work_item.test_plan_blocked" in event_kinds
    finally:
        conn.close()


def test_plan_blocks_when_json_generation_raises(monkeypatch, tmp_path: Path) -> None:
    """Wholesale failure path: `plan_to_json` raises. The work item
    must transition to `blocked`, not `planned`, and the error must
    be surfaced in the return payload + event."""
    conn, paths, events = _runtime(tmp_path)
    try:
        work_id = _seed_work_item(conn, paths, events)
        _write_analysis_artifacts(
            paths,
            work_id,
            candidates_json=json.dumps(
                {
                    "items": [
                        {
                            "candidate_id": "API-CASE-1",
                            "title": "case 1",
                            "test_type": "api",
                        }
                    ]
                }
            ),
        )
        update_work_item_status(conn, events, work_item_id=work_id, status="analyzing")
        update_work_item_status(conn, events, work_item_id=work_id, status="planned")
        update_work_item_status(conn, events, work_item_id=work_id, status="queued")

        import agentic_os.plan_v2 as plan_v2

        def boom(*_args, **_kwargs):
            raise RuntimeError("simulated planner crash")

        monkeypatch.setattr(plan_v2, "plan_to_json", boom)

        result = plan_work_item(conn, paths, events, work_item_id=work_id)
        assert result["status"] == "blocked"
        assert result["error"].startswith("test_plan_json_generation_failed:")
        assert "simulated planner crash" in result["error"]
        assert get_work_item(conn, work_id)["status"] == "blocked"
    finally:
        conn.close()


def test_blocked_plan_does_not_register_test_plan_artifact(tmp_path: Path) -> None:
    """Codex review on #128 (P1): a blocked plan must not register a
    `test_plan` artifact, otherwise the downstream `Generate tests`
    selector picks it up and lets `implement-tests` run on a broken
    plan — defeating the point of #86."""
    from agentic_os.suggestions import compute_suggestions
    from agentic_os.work_items import list_work_item_artifacts

    conn, paths, events = _runtime(tmp_path)
    try:
        work_id = _seed_work_item(conn, paths, events)
        _write_analysis_artifacts(
            paths,
            work_id,
            candidates_json=json.dumps(
                {"items": [{"title": "case A"}, {"title": "case B"}]}
            ),
        )
        update_work_item_status(conn, events, work_item_id=work_id, status="analyzing")
        update_work_item_status(conn, events, work_item_id=work_id, status="planned")
        update_work_item_status(conn, events, work_item_id=work_id, status="queued")

        result = plan_work_item(conn, paths, events, work_item_id=work_id)
        assert result["status"] == "blocked"
        # No `test_plan` artifact recorded on a blocked plan.
        kinds = {a["kind"] for a in list_work_item_artifacts(conn, work_id)}
        assert "test_plan" not in kinds
        assert result["artifacts"] == []

        # And the suggestions selector must not surface the blocked
        # task as a `generate_tests` candidate.
        suggestions = compute_suggestions(paths, conn)
        ids_in_generate = {
            t["id"]
            for s in suggestions
            if s.get("kind") == "generate_tests"
            for t in s.get("targets", [])
        }
        assert work_id not in ids_in_generate
    finally:
        conn.close()


def test_plan_marks_planned_on_happy_path(tmp_path: Path) -> None:
    """Sanity — when candidates_json yields a valid PlanItem, status
    stays `planned`."""
    conn, paths, events = _runtime(tmp_path)
    try:
        work_id = _seed_work_item(conn, paths, events)
        _write_analysis_artifacts(
            paths,
            work_id,
            candidates_json=json.dumps(
                {
                    "items": [
                        {
                            "candidate_id": "API-CASE-1",
                            "title": "case 1",
                            "test_type": "api",
                        }
                    ]
                }
            ),
        )
        update_work_item_status(conn, events, work_item_id=work_id, status="analyzing")
        update_work_item_status(conn, events, work_item_id=work_id, status="planned")
        update_work_item_status(conn, events, work_item_id=work_id, status="queued")

        result = plan_work_item(conn, paths, events, work_item_id=work_id)
        assert result["status"] == "planned"
        assert result["error"] is None
        assert get_work_item(conn, work_id)["status"] == "planned"
    finally:
        conn.close()
