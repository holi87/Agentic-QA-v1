"""Issue #193/#195/#196/#199/#202 — dashboard cockpit API contracts.

The monitoring cockpit reads three new endpoints:

- `GET /api/dashboard/overview` — counts + latest run + current process
  + next operator action. This is the single source of truth that
  drives the home page cards.
- `GET /api/dashboard/preflight` — readiness checklist with pass/warn/
  fail states and remediation hints.
- `GET /api/dashboard/charts` — pre-aggregated arrays for inline SVG
  charts (run history, funnel, failure trend, bugs/blockers).

These tests pin the shape so the UI and any external consumer can rely
on the keys.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agentic_os.dashboard import (
    aggregate_candidate_debt,
    build_charts,
    build_overview,
    build_preflight,
    current_process,
    fetch_active_runs,
    fetch_recent_runs,
    next_operator_action,
)
from agentic_os.events import EventLog
from agentic_os.ids import ulid
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db
from agentic_os.work_items import create_work_item_from_payload
from agentic_os.work_items import register_work_item_artifact


def _runtime(tmp_path: Path) -> tuple[sqlite3.Connection, RuntimePaths, EventLog, Orchestrator]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    return conn, paths, events, orch


def _seed_work_item(conn, paths, events, *, title="Overview WI", priority="P1") -> str:
    detail = create_work_item_from_payload(
        conn,
        paths,
        events,
        {
            "title": title,
            "spec_path": f"specs/{title.lower().replace(' ', '-')}.md",
            "priority": priority,
            "sut_root": ".",
            "scenarios": ["smoke"],
        },
        default_sut_root=".",
    )
    return str(detail["work_item"]["id"])


def _write_plan(paths: RuntimePaths, work_item_id: str, summary: dict) -> None:
    plan_dir = paths.runtime_root / "plans" / work_item_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "TEST-PLAN.json").write_text(
        json.dumps({"summary": summary, "items": []}), encoding="utf-8"
    )


def _register_artifact(conn, paths, events, work_item_id: str, kind: str, rel_path: str) -> None:
    target = paths.repo_root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(f"# {kind}\n", encoding="utf-8")
    register_work_item_artifact(
        conn,
        paths,
        events,
        work_item_id=work_item_id,
        kind=kind,
        path=rel_path,
    )


def _register_approved_patch(conn, paths, events, work_item_id: str) -> str:
    patch_rel = f".agentic-os/patches/{work_item_id}/abc.patch"
    _register_artifact(conn, paths, events, work_item_id, "patch", patch_rel)
    gate_rel = f".agentic-os/gates/{work_item_id}-approved.txt"
    gate_path = paths.repo_root / gate_rel
    gate_path.parent.mkdir(parents=True, exist_ok=True)
    gate_path.write_text(
        f"verdict: APPROVE\nreason: test approval\npatch: {patch_rel}\n",
        encoding="utf-8",
    )
    register_work_item_artifact(
        conn,
        paths,
        events,
        work_item_id=work_item_id,
        kind="gate",
        path=gate_rel,
    )
    return patch_rel


# ---------------------------------------------------------------------------
# Overview shape
# ---------------------------------------------------------------------------


def test_overview_returns_full_shape_on_empty_runtime(tmp_path: Path) -> None:
    conn, paths, _events, _orch = _runtime(tmp_path)
    try:
        payload = build_overview(conn, paths)
        # Every documented key must be present so the JS never branches
        # on `if (overview.foo)` defensively.
        for key in (
            "work_items",
            "candidates",
            "generated_tests",
            "latest_run",
            "current_process",
            "next_action",
            "bugs",
            "blockers",
        ):
            assert key in payload, f"missing key: {key}; payload={payload!r}"
        assert payload["latest_run"] is None
        assert payload["current_process"] is None
        assert payload["next_action"] is None
    finally:
        conn.close()


def test_overview_counts_candidate_debt_across_work_items(tmp_path: Path) -> None:
    conn, paths, events, _orch = _runtime(tmp_path)
    try:
        wid_a = _seed_work_item(conn, paths, events, title="A")
        wid_b = _seed_work_item(conn, paths, events, title="B")
        _write_plan(
            paths,
            wid_a,
            {
                "total": 5,
                "generate_now": 3,
                "needs_operator_decision": 1,
                "not_testable": 1,
                "blocked_missing_docs": 0,
            },
        )
        _write_plan(
            paths,
            wid_b,
            {
                "total": 4,
                "generate_now": 2,
                "needs_operator_decision": 2,
                "not_testable": 0,
                "blocked_missing_docs": 0,
            },
        )
        payload = build_overview(conn, paths)
        cands = payload["candidates"]
        assert cands["total"] == 9
        assert cands["generate_now"] == 5
        assert cands["needs_operator_decision"] == 3
        assert cands["not_testable"] == 1
    finally:
        conn.close()


def test_overview_next_action_prioritizes_pending_decisions(tmp_path: Path) -> None:
    conn, paths, events, _orch = _runtime(tmp_path)
    try:
        wid = _seed_work_item(conn, paths, events, title="Needs review")
        _write_plan(
            paths,
            wid,
            {
                "total": 3,
                "generate_now": 0,
                "needs_operator_decision": 3,
                "not_testable": 0,
                "blocked_missing_docs": 0,
            },
        )
        payload = build_overview(conn, paths)
        next_action = payload["next_action"]
        assert next_action is not None
        assert next_action["work_item_id"] == wid
        assert "decision" in next_action["action"].lower() or "review" in next_action["action"].lower()
    finally:
        conn.close()


def test_overview_next_action_prioritizes_patch_apply_over_done_candidate_debt(
    tmp_path: Path,
) -> None:
    conn, paths, events, _orch = _runtime(tmp_path)
    try:
        done_wid = _seed_work_item(conn, paths, events, title="Old debt")
        active_wid = _seed_work_item(conn, paths, events, title="Approved patch")
        conn.execute("UPDATE work_items SET status='done' WHERE id=?;", (done_wid,))
        conn.execute("UPDATE work_items SET status='reviewing' WHERE id=?;", (active_wid,))
        conn.commit()
        _write_plan(
            paths,
            done_wid,
            {
                "total": 1,
                "generate_now": 0,
                "needs_operator_decision": 1,
                "not_testable": 0,
                "blocked_missing_docs": 0,
            },
        )
        _register_approved_patch(conn, paths, events, active_wid)

        payload = build_overview(conn, paths)
        next_action = payload["next_action"]
        assert next_action is not None
        assert next_action["work_item_id"] == active_wid
        assert next_action["action"] == "Apply approved patch"
    finally:
        conn.close()


def test_generated_tests_metric_counts_patch_manifest_files(tmp_path: Path) -> None:
    conn, paths, _events, _orch = _runtime(tmp_path)
    try:
        manifest_dir = paths.patches_dir / "TASK-1" / "RUN-1"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "version": "1.0",
                    "files": [
                        {"relative_path": "tests/api/c1-health.spec.ts"},
                        {"relative_path": "tests/ui/c2-login.spec.ts"},
                        {"relative_path": "tests/ui/c2-login.spec.ts"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        generated = build_overview(conn, paths)["generated_tests"]
        assert generated["source"] == "patch_manifests"
        assert generated["total"] == 2
        assert generated["api"] == 1
        assert generated["ui"] == 1
        assert generated["other"] == 0
    finally:
        conn.close()


def test_latest_run_includes_report_counts_when_manifest_finalized_reports(
    tmp_path: Path,
) -> None:
    conn, paths, _events, _orch = _runtime(tmp_path)
    try:
        from agentic_os.orchestrator import CURRENT_PHASE_ID

        task_id = ulid()
        run_id = ulid()
        now = "2026-05-26T04:19:56.833Z"
        manifest_rel = f".agentic-os/runs/{run_id}/manifest.json"
        manifest_path = paths.repo_root / manifest_rel
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps({"kind": "run-tests", "reports": {"path": "reports", "finalized": True}}),
            encoding="utf-8",
        )
        reports = paths.repo_root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "last-run.json").write_text(
            json.dumps(
                {
                    "total": 4,
                    "passed": 3,
                    "failed": 1,
                    "skipped": 0,
                    "failures": [{"name": "known", "tags": ["@known-bug", "@bug-001"]}],
                }
            ),
            encoding="utf-8",
        )
        conn.execute(
            """
            INSERT INTO tasks(id, phase_id, parent_id, kind, status, payload, created_at, updated_at)
            VALUES (?, ?, NULL, 'run', 'succeeded', '{}', ?, ?);
            """,
            (task_id, CURRENT_PHASE_ID, now, now),
        )
        conn.execute(
            """
            INSERT INTO runs(id, task_id, command, cwd, env_hash, log_path,
                             started_at, finished_at, exit_code, failure_kind, manifest_path)
            VALUES (?, ?, '["agentic-os","run","run-tests"]', ?, 'h', ?, ?, ?, 1, 'product', ?);
            """,
            (
                run_id,
                task_id,
                str(paths.repo_root),
                f".agentic-os/subprocess-logs/{run_id}.log",
                now,
                now,
                manifest_rel,
            ),
        )
        conn.commit()

        latest = build_overview(conn, paths)["latest_run"]
        assert latest["report_counts_available"] is True
        assert latest["total"] == 4
        assert latest["passed"] == 3
        assert latest["failed"] == 1
        assert latest["known_bug"] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Current process — issue #196
# ---------------------------------------------------------------------------


def _seed_active_run(conn, paths, *, task_kind="run", work_item_id="WI-001", workflow="run-tests") -> str:
    """Insert a task + run row that simulates a long action mid-flight."""
    from agentic_os.orchestrator import CURRENT_PHASE_ID

    task_id = ulid()
    run_id = ulid()
    now = "2026-05-25T22:00:00.000Z"
    payload = json.dumps({"workflow": workflow, "work_item_id": work_item_id})
    conn.execute(
        """
        INSERT INTO tasks(id, phase_id, parent_id, kind, status, payload, created_at, updated_at)
        VALUES (?, ?, NULL, ?, 'running', ?, ?, ?);
        """,
        (task_id, CURRENT_PHASE_ID, task_kind, payload, now, now),
    )
    conn.execute(
        """
        INSERT INTO runs(id, task_id, command, cwd, env_hash, log_path, started_at)
        VALUES (?, ?, ?, ?, ?, ?, ?);
        """,
        (
            run_id,
            task_id,
            '["agentic-os","run","run-tests"]',
            str(paths.repo_root),
            "deadbeef",
            f".agentic-os/subprocess-logs/{run_id}.log",
            now,
        ),
    )
    conn.commit()
    return run_id


def test_current_process_returns_none_when_idle(tmp_path: Path) -> None:
    conn, paths, _events, _orch = _runtime(tmp_path)
    try:
        assert current_process(conn) is None
        assert fetch_active_runs(conn) == []
    finally:
        conn.close()


def test_current_process_exposes_work_item_and_workflow(tmp_path: Path) -> None:
    conn, paths, _events, _orch = _runtime(tmp_path)
    try:
        run_id = _seed_active_run(conn, paths)
        cur = current_process(conn)
        assert cur is not None
        assert cur["run_id"] == run_id
        assert cur["work_item_id"] == "WI-001"
        assert cur["workflow"] == "run-tests"
        assert cur["active_count"] == 1
        assert cur["log_path"].endswith(f"{run_id}.log")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Preflight — issue #199
# ---------------------------------------------------------------------------


def test_preflight_returns_structured_checks(tmp_path: Path) -> None:
    conn, paths, _events, _orch = _runtime(tmp_path)
    try:
        payload = build_preflight(conn, paths)
        assert "ok" in payload and "checks" in payload
        ids = {c["id"] for c in payload["checks"]}
        # Dashboard-layer checks must always appear, regardless of
        # whether autonomy preflight passes.
        assert "runtime_db_integrity" in ids
        assert "dashboard_write_mode" in ids
        # Every check has the documented shape.
        for c in payload["checks"]:
            assert {"id", "status", "message", "actions"}.issubset(set(c))
            assert c["status"] in {"pass", "warn", "fail"}
    finally:
        conn.close()


def test_preflight_uses_effective_write_mode_override(tmp_path: Path) -> None:
    conn, paths, _events, _orch = _runtime(tmp_path)
    try:
        payload = build_preflight(conn, paths, write_enabled=True)
        write_check = next(c for c in payload["checks"] if c["id"] == "dashboard_write_mode")
        assert write_check["status"] == "pass"
        assert write_check["message"] == "dashboard writes enabled"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Charts — issue #195
# ---------------------------------------------------------------------------


def test_charts_payload_has_pinned_keys(tmp_path: Path) -> None:
    conn, paths, _events, _orch = _runtime(tmp_path)
    try:
        payload = build_charts(conn, paths)
        for key in ("run_history", "failure_trend", "funnel", "bugs", "blockers"):
            assert key in payload, f"missing chart key: {key}"
        assert isinstance(payload["run_history"], list)
        assert isinstance(payload["failure_trend"], dict)
        assert "planned" in payload["funnel"]
    finally:
        conn.close()


def test_recent_runs_orders_newest_first(tmp_path: Path) -> None:
    conn, paths, _events, _orch = _runtime(tmp_path)
    try:
        from agentic_os.orchestrator import CURRENT_PHASE_ID

        # Seed three finished runs with monotonic timestamps.
        task_id = ulid()
        now = "2026-05-25T22:00:00.000Z"
        conn.execute(
            """
            INSERT INTO tasks(id, phase_id, parent_id, kind, status, payload, created_at, updated_at)
            VALUES (?, ?, NULL, 'run', 'succeeded', '{}', ?, ?);
            """,
            (task_id, CURRENT_PHASE_ID, now, now),
        )
        for idx, finished in enumerate(
            ["2026-05-25T22:05:00.000Z", "2026-05-25T22:10:00.000Z", "2026-05-25T22:15:00.000Z"]
        ):
            conn.execute(
                """
                INSERT INTO runs(id, task_id, command, cwd, env_hash, log_path,
                                 started_at, finished_at, exit_code, failure_kind)
                VALUES (?, ?, '["x"]', ?, 'h', ?, ?, ?, ?, ?);
                """,
                (
                    ulid(),
                    task_id,
                    str(paths.repo_root),
                    f"l{idx}",
                    now,
                    finished,
                    0 if idx % 2 == 0 else 1,
                    None if idx % 2 == 0 else "product",
                ),
            )
        conn.commit()
        runs = fetch_recent_runs(conn, limit=10)
        assert [r["finished_at"] for r in runs] == [
            "2026-05-25T22:15:00.000Z",
            "2026-05-25T22:10:00.000Z",
            "2026-05-25T22:05:00.000Z",
        ]
    finally:
        conn.close()


def test_next_operator_action_falls_back_to_oldest_queued(tmp_path: Path) -> None:
    items = [
        {
            "id": "WI-2",
            "title": "Later",
            "status": "queued",
            "candidate_debt": {"needs_operator_decision": 0},
        },
        {
            "id": "WI-1",
            "title": "First",
            "status": "queued",
            "candidate_debt": {"needs_operator_decision": 0},
        },
    ]
    nxt = next_operator_action(items)
    assert nxt is not None
    assert nxt["work_item_id"] == "WI-2"  # first in list is the picked one
    assert "Start" in nxt["action"]


def test_aggregate_debt_zero_filled_on_empty(tmp_path: Path) -> None:
    conn, paths, _events, _orch = _runtime(tmp_path)
    try:
        totals = aggregate_candidate_debt(paths, [])
        assert totals == {
            "total": 0,
            "generate_now": 0,
            "needs_operator_decision": 0,
            "not_testable": 0,
            "blocked_missing_docs": 0,
        }
    finally:
        conn.close()
