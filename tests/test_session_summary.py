"""Issue #272 — session summary artifact (render, scoping, idempotent write)."""
from __future__ import annotations

import json
from pathlib import Path

from agentic_os import sessions as sessions_mod
from agentic_os import summaries
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db
from agentic_os.storage.db import transaction

SID = "autonomy-test272"
STARTED = "2026-05-26T10:00:00Z"
FINISHED = "2026-05-26T11:00:00Z"
IN_WINDOW = "2026-05-26T10:30:00.000Z"  # millisecond precision, inside window
BEFORE = "2026-05-26T09:00:00Z"
AFTER = "2026-05-26T12:00:00Z"


def _conn(tmp_path: Path):
    paths = RuntimePaths(repo_root=tmp_path / "repo", runtime_root=tmp_path / "repo" / ".agentic-os")
    paths.ensure()
    return init_db(paths.db), paths


def _seed_work_item(conn, wid, status, ts):
    conn.execute(
        "INSERT INTO work_items(id,title,status,spec_path,sut_root,priority,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?);",
        (wid, f"title {wid}", status, "spec.md", "/sut", "P1", ts, ts),
    )


def _seed_run(conn, phase_id, task_id, run_id, ts):
    conn.execute(
        "INSERT INTO phases(id,status,branch,spec_path,updated_at) VALUES (?,?,?,?,?);",
        (phase_id, "in_progress", "task/x", "spec.md", ts),
    )
    conn.execute(
        "INSERT INTO tasks(id,phase_id,kind,status,payload,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?);",
        (task_id, phase_id, "run", "succeeded", "{}", ts, ts),
    )
    conn.execute(
        "INSERT INTO runs(id,task_id,command,cwd,env_hash,log_path,started_at)"
        " VALUES (?,?,?,?,?,?,?);",
        (run_id, task_id, "[]", "/cwd", "hash", "log.txt", ts),
    )


def _seed_test_result(conn, rid, run_id, status, ts):
    conn.execute(
        "INSERT INTO test_results(id,run_id,scenario_name,feature_path,status,functional_tag,all_tags,created_at)"
        " VALUES (?,?,?,?,?,?,?,?);",
        (rid, run_id, "scn", "feat.feature", status, "@api", "[]", ts),
    )


def _build_synthetic(conn):
    """Session with 3 work items, 1 bug, tests + provider activity + decisions."""
    sessions_mod.record_session_start(
        conn, session_id=SID, started_at=STARTED, mode="loop", max_minutes=60
    )
    sessions_mod.finalize_session(
        conn, session_id=SID, status="finished", finished_at=FINISHED,
        work_items_processed=3, blocks=0, failures=1,
    )
    _seed_work_item(conn, "WI-1", "done", IN_WINDOW)
    _seed_work_item(conn, "WI-2", "done", IN_WINDOW)
    _seed_work_item(conn, "WI-3", "blocked", IN_WINDOW)
    # A work item touched outside the window must NOT be counted.
    _seed_work_item(conn, "WI-OLD", "done", BEFORE)

    _seed_run(conn, "PH-1", "T-1", "R-1", IN_WINDOW)
    _seed_test_result(conn, "TR-1", "R-1", "passed", IN_WINDOW)
    _seed_test_result(conn, "TR-2", "R-1", "passed", IN_WINDOW)
    _seed_test_result(conn, "TR-3", "R-1", "failed", IN_WINDOW)
    _seed_test_result(conn, "TR-OUT", "R-1", "passed", AFTER)  # out of window

    conn.execute(
        "INSERT INTO bugs(id,scenario_tag,severity,status,evidence_dir,first_seen,last_seen)"
        " VALUES (?,?,?,?,?,?,?);",
        ("BUG-042", "@bug-042", "P1", "open", "bugs/BUG-042", IN_WINDOW, IN_WINDOW),
    )

    conn.execute(
        "INSERT INTO model_invocations(id,session_id,model_role,provider,command,started_at,tokens_in,tokens_out,cost_usd)"
        " VALUES (?,?,?,?,?,?,?,?,?);",
        ("MI-1", SID, "opus", "claude", "[]", IN_WINDOW, 100, 42, 2.10),
    )
    conn.execute(
        "INSERT INTO model_invocations(id,session_id,model_role,provider,command,started_at,tokens_in,tokens_out,cost_usd)"
        " VALUES (?,?,?,?,?,?,?,?,?);",
        ("MI-2", "other-session", "codex", "codex", "[]", IN_WINDOW, 999, 999, 9.99),
    )

    conn.execute(
        "INSERT INTO decisions(id,phase_id,topic,decided_by,rationale,consequences,decided_at,actor)"
        " VALUES (?,?,?,?,?,?,?,?);",
        ("DEC-1", "PH-1", "generate_now WI-2", "opus", "candidate ok", "tests generated",
         IN_WINDOW, "planner-autopilot"),
    )


def test_headline_counts(tmp_path: Path) -> None:
    conn, _ = _conn(tmp_path)
    try:
        with transaction(conn):
            _build_synthetic(conn)
        data = summaries.build_session_summary(conn, SID)
        h = data["headline"]
        assert h["work_items_processed"] == 3
        assert h["blocks"] == 0
        assert h["failures"] == 1
        assert (h["tests_total"], h["tests_passed"], h["tests_failed"]) == (3, 2, 1)
        assert h["bugs_filed"] == 1
        # outcome: failures>0 -> partial
        assert data["outcome"] == "partial"
        # provider activity scoped to this session only (MI-2 excluded)
        assert [p["role"] for p in data["providers"]] == ["opus"]
        assert data["providers"][0]["tokens"] == 142
        assert data["providers"][0]["cost_usd"] == 2.10
        assert data["budget_consumed"] == {"tokens": 142, "usd": 2.10}
        # decisions scoped to window
        assert [d["id"] for d in data["decisions"]] == ["DEC-1"]
    finally:
        conn.close()


def test_markdown_sections(tmp_path: Path) -> None:
    conn, _ = _conn(tmp_path)
    try:
        with transaction(conn):
            _build_synthetic(conn)
        md = summaries.render_session_summary(conn, SID)
        assert "## Headline" in md
        assert "## Per work item" in md
        assert "## Open items" in md
        # 3 in-window work items present, the out-of-window one absent
        assert "WI-1" in md and "WI-2" in md and "WI-3" in md
        assert "WI-OLD" not in md
        # the filed bug surfaces under open items
        assert "BUG-042" in md
        # frontmatter + heading
        assert md.startswith("---\n")
        assert f"# Session summary — {SID}" in md
    finally:
        conn.close()


def test_render_is_deterministic(tmp_path: Path) -> None:
    conn, _ = _conn(tmp_path)
    try:
        with transaction(conn):
            _build_synthetic(conn)
        first = summaries.render_session_summary(conn, SID)
        second = summaries.render_session_summary(conn, SID)
        assert first == second
    finally:
        conn.close()


def test_processed_work_items_override(tmp_path: Path) -> None:
    conn, _ = _conn(tmp_path)
    try:
        with transaction(conn):
            _build_synthetic(conn)
        # Explicit list wins over window derivation, including an out-of-window id.
        data = summaries.build_session_summary(
            conn, SID, processed_work_items=["WI-1", "WI-OLD"]
        )
        assert sorted(w["id"] for w in data["work_items"]) == ["WI-1", "WI-OLD"]
    finally:
        conn.close()


def test_write_is_idempotent(tmp_path: Path) -> None:
    conn, paths = _conn(tmp_path)
    try:
        with transaction(conn):
            _build_synthetic(conn)
        reports = paths.repo_root / "reports"
        p1 = summaries.write_session_summary(conn, SID, reports_dir=reports)
        body1 = p1.read_text(encoding="utf-8")
        p2 = summaries.write_session_summary(conn, SID, reports_dir=reports)
        body2 = p2.read_text(encoding="utf-8")
        assert p1 == p2 == reports / f"session-summary-{SID}.md"
        assert body1 == body2
        assert summaries.summary_relpath(SID) == f"reports/session-summary-{SID}.md"
    finally:
        conn.close()


def test_missing_session_returns_none(tmp_path: Path) -> None:
    conn, paths = _conn(tmp_path)
    try:
        assert summaries.build_session_summary(conn, "nope") is None
        assert summaries.render_session_summary(conn, "nope") is None
        assert summaries.write_session_summary(
            conn, "nope", reports_dir=paths.repo_root / "reports"
        ) is None
    finally:
        conn.close()


def test_outcome_mapping() -> None:
    assert summaries._derive_outcome("finished", 0, 0) == "ok"
    assert summaries._derive_outcome("finished", 2, 0) == "blocked"
    assert summaries._derive_outcome("finished", 0, 1) == "partial"
    assert summaries._derive_outcome("finished", 1, 1) == "partial"
    assert summaries._derive_outcome("failed", 0, 0) == "partial"


def test_cli_sessions_summary(tmp_path: Path, capsys) -> None:
    from agentic_os.cli import cmd_sessions

    from test_dashboard_server import _runtime  # type: ignore[import-not-found]

    paths = _runtime(tmp_path)
    conn = init_db(paths.db)
    with transaction(conn):
        _build_synthetic(conn)
    conn.close()

    rc = cmd_sessions(paths.repo_root, ["summary", SID], json_output=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "## Headline" in out and "WI-3" in out and "BUG-042" in out

    rc = cmd_sessions(paths.repo_root, ["summary", SID, "--json"], json_output=False)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == SID
    assert payload["headline"]["work_items_processed"] == 3

    rc = cmd_sessions(paths.repo_root, ["summary", "missing"], json_output=False)
    assert rc == 4
