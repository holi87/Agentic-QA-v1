"""Issue #110 — known-bug triage must verify @bug-NNN against real records.

Self-declared `@known-bug @bug-NNN` pairs are not authoritative. Triage
must resolve the tag against `bugs/BUG-NNN-*.md` or the SQLite `bugs`
registry. Unresolved claims fall through to `product_bug` classification
(per the existing exact-spec policy) and a policy-violation flag is
recorded so the dashboard can surface the false-known-bug attempt.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agentic_os.events import EventLog
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db
from agentic_os.workflows import _resolve_known_bug_tags


def _runtime(tmp_path: Path) -> tuple[sqlite3.Connection, RuntimePaths, EventLog]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    return conn, paths, events


def test_resolves_when_bug_markdown_exists(tmp_path: Path) -> None:
    conn, paths, _events = _runtime(tmp_path)
    try:
        bugs_dir = paths.repo_root / "bugs"
        bugs_dir.mkdir(parents=True, exist_ok=True)
        (bugs_dir / "BUG-001-orders-422.md").write_text(
            "# BUG-001\nstatus: known\n", encoding="utf-8"
        )
        resolved, unresolved = _resolve_known_bug_tags(paths, {"@bug-001"})
        assert resolved == {"@bug-001"}
        assert unresolved == set()
    finally:
        conn.close()


def test_resolves_when_sqlite_bug_row_exists(tmp_path: Path) -> None:
    conn, paths, _events = _runtime(tmp_path)
    try:
        conn.execute(
            "INSERT INTO bugs(id, scenario_tag, severity, status, evidence_dir, first_seen, last_seen)"
            " VALUES (?, ?, ?, ?, ?, ?, ?);",
            (
                "BUG-002",
                "@functional-orders",
                "P1",
                "known",
                "bugs/BUG-002",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        resolved, unresolved = _resolve_known_bug_tags(paths, {"@bug-002"})
        assert resolved == {"@bug-002"}
        assert unresolved == set()
    finally:
        conn.close()


def test_unresolved_when_no_bug_file_or_row(tmp_path: Path) -> None:
    conn, paths, _events = _runtime(tmp_path)
    try:
        resolved, unresolved = _resolve_known_bug_tags(paths, {"@bug-999"})
        assert resolved == set()
        assert unresolved == {"@bug-999"}
    finally:
        conn.close()


def test_malformed_bug_tag_is_unresolved(tmp_path: Path) -> None:
    conn, paths, _events = _runtime(tmp_path)
    try:
        # The triage producer prefilters on `@bug-\d+`, but the helper is
        # called from other code paths too. A malformed tag must never
        # parse as resolved.
        resolved, unresolved = _resolve_known_bug_tags(
            paths, {"@bug-abc", "@bug-", "@bug-001abc"}
        )
        assert resolved == set()
        assert "@bug-abc" in unresolved
        assert "@bug-" in unresolved
        # "@bug-001abc" starts with a numeric prefix; the helper matches
        # the leading digits but the resulting bug id has no record, so
        # it remains unresolved.
        assert "@bug-001abc" in unresolved
    finally:
        conn.close()


def _seed_run(conn: sqlite3.Connection, orch, run_id: str) -> None:
    task_id = orch.create_task(
        phase_id="14-sonnet-dashboard-run-e2e", kind="review", payload={"workflow": "test"}
    )
    orch.lease_task(task_id, owner="orchestrator", ttl_seconds=60)
    orch.mark_running(task_id, owner="orchestrator")
    orch.record_run(
        task_id=task_id,
        run_id=run_id,
        command=["agentic-os", "test"],
        cwd=".",
        env_hash="x",
        log_path=".agentic-os/logs/x.log",
        started_at="2026-01-01T00:00:00Z",
    )


def test_triage_flags_self_declared_unresolved_known_bug(tmp_path: Path) -> None:
    """End-to-end: a failure with `@known-bug @bug-999` but no matching
    record must NOT be classified as `known_bug_red`. The triage payload
    records `known_bug_policy_violation` and an event is emitted."""
    from agentic_os.orchestrator import Orchestrator
    from agentic_os.workflows import triage_reports

    conn, paths, events = _runtime(tmp_path)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    _seed_run(conn, orch, "run-test")
    try:
        reports = paths.repo_root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "last-run.json").write_text(
            json.dumps(
                {
                    "ran_at": "2026-01-01T00:00:00Z",
                    "total": 1,
                    "passed": 0,
                    "failed": 1,
                    "skipped": 0,
                    "failures": [
                        {
                            "scenario": "fake known bug claim",
                            "tags": ["@known-bug", "@bug-999"],
                            "error_message": "AssertionError",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (reports / "summary.md").write_text("# stub\n", encoding="utf-8")

        result = triage_reports(paths, events, run_id_str="run-test", auto_file_bugs=False)
        assert result["available"] is True
        # Self-declared `@bug-999` without a bug file/row must not yield
        # `known_bug_red`.
        assert result["summary"].get("known_bug_red", 0) == 0
        assert any(item["category"] == "product_bug" for item in result["items"])
        violator = next(
            item for item in result["items"] if item["name"] == "fake known bug claim"
        )
        assert violator["known_bug_policy_violation"] is True
        assert violator["unresolved_known_bug_tags"] == ["@bug-999"]
        event_kinds = [e["kind"] for e in events.tail(200)]
        assert "triage.known_bug_unresolved" in event_kinds
    finally:
        conn.close()


def test_untagged_product_failure_is_product_bug(tmp_path: Path) -> None:
    """No `@known-bug`, no `@bug-NNN` tag ⇒ product_bug, no policy violation."""
    from agentic_os.orchestrator import Orchestrator
    from agentic_os.workflows import triage_reports

    conn, paths, events = _runtime(tmp_path)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    _seed_run(conn, orch, "run-test")
    try:
        reports = paths.repo_root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "last-run.json").write_text(
            json.dumps(
                {
                    "ran_at": "2026-01-01T00:00:00Z",
                    "total": 1,
                    "passed": 0,
                    "failed": 1,
                    "skipped": 0,
                    "failures": [
                        {
                            "scenario": "checkout rejects invalid card",
                            "tags": ["@functional-orders"],
                            "error_message": "AssertionError",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (reports / "summary.md").write_text("# stub\n", encoding="utf-8")

        result = triage_reports(paths, events, run_id_str="run-test", auto_file_bugs=False)
        item = result["items"][0]
        assert item["category"] == "product_bug"
        assert item["known_bug_policy_violation"] is False
        assert item["unresolved_known_bug_tags"] == []
    finally:
        conn.close()
