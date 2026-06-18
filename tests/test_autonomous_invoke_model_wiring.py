"""Regression — issue #308.

The autonomous loop must now drive ``invoke_model`` from the planning
step so ``model_invocations`` rows actually land (keyed to the session)
and ``budget_status`` reflects real cost.

Before #308, ``plan_work_item`` was a deterministic artefact builder
with no model call. ``invoke_model`` had zero non-test callers and
``model_invocations.cost_usd`` was always 0 at runtime. These tests pin
the wiring so a future refactor cannot silently re-break it.
"""
from __future__ import annotations

import inspect
import json
import os
import sqlite3
from pathlib import Path

import pytest

from agentic_os.analysis import analyze_work_item
from agentic_os.autonomy import _SessionState, _autonomy_step
from agentic_os.budgets import budget_status
from agentic_os.events import EventLog
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db
from agentic_os.test_planning import plan_work_item


_FAKE_PLANNER_STDOUT = (
    "# planner-note\n\nAdvisory — please review the bucket priorities.\n"
)


def _runtime(tmp_path: Path):
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    return conn, paths, events, repo


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o755)


def _install_planner_config(repo: Path, fake_planner: Path) -> None:
    """Drop a canonical config + non-zero provider rates so ``_cost_usd``
    returns > 0 when the fake planner emits any output.

    Starts from the in-repo example so every key the config validator
    requires is present, then rewrites ``models.planner.command`` to
    point at the fake binary and ``runtime.root`` at the tmp_path
    runtime directory. The fake planner runs synchronously; other roles
    are not invoked by ``plan_work_item``.
    """
    import yaml  # PyYAML is a runtime dep of agentic_os.

    src = Path(__file__).resolve().parent.parent / "config" / "agentic-os.yml.example"
    cfg = yaml.safe_load(src.read_text(encoding="utf-8"))
    # Keep the example's relative runtime root (validator rejects absolute);
    # RuntimePaths in the test is constructed directly, so this string only
    # has to satisfy validation, not steer file IO.
    cfg["models"]["planner"]["provider"] = "claude"
    cfg["models"]["planner"]["command"] = [str(fake_planner)]
    cfg["models"]["planner"]["role"] = "opus"
    # Drop fallbacks so a fake-script failure (or success) never tries
    # the real `codex` / `agy` binaries inside the test process.
    cfg["models"]["planner"].pop("fallback", None)

    cfg_dir = repo / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "agentic-os.yml").write_text(
        yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8"
    )
    (cfg_dir / "provider-rates.yml").write_text(
        "claude:\n"
        "  input_per_1k_usd: 0.003\n"
        "  output_per_1k_usd: 0.015\n",
        encoding="utf-8",
    )


def _seed_work_item(conn: sqlite3.Connection, paths: RuntimePaths) -> str:
    """Persist a work item + populate the analysis artefacts that
    ``plan_work_item`` reads. Bypasses the spec-on-disk path the CLI
    uses, so the test stays focused on the model wire-in."""
    work_id = "TASK-20260528-000000-issue-308"
    spec_dir = paths.runtime_root / "tasks" / work_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spec_dir / "TASK.md"
    spec_path.write_text("# Sample task\n\nSomething to test.\n", encoding="utf-8")
    rel_spec = str(spec_path.resolve().relative_to(paths.repo_root.resolve()))

    conn.execute(
        "INSERT INTO work_items(id, project_id, title, priority, sut_root, "
        "spec_path, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
        (
            work_id,
            "default",
            "Sample task",
            "P2",
            ".",
            rel_spec,
            "analyzing",
            "2026-05-28T00:00:00Z",
            "2026-05-28T00:00:00Z",
        ),
    )
    conn.commit()

    analysis_dir = paths.runtime_root / "analysis" / work_id
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / "requirements.md").write_text(
        "- Login flow must succeed for valid credentials.\n", encoding="utf-8"
    )
    (analysis_dir / "candidate-tests.md").write_text(
        "## API\n- POST /login returns 200 for valid user\n", encoding="utf-8"
    )
    (analysis_dir / "candidate-tests.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "candidate_id": "API-LOGIN-1",
                        "title": "POST /login valid",
                        "test_type": "api",
                        "priority": "P2",
                        "decision": "needs_operator_decision",
                        "expected_assertion": "200 OK",
                        "source_refs": ["spec#login"],
                        "target_method": "POST",
                        "target_path": "/login",
                        "required_test_data": "valid user",
                        "cleanup_strategy": "n/a",
                        "generator_target": "playwright-ts",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (analysis_dir / "sut-map.json").write_text(
        json.dumps({"config_snapshot": {}, "openapi_inventory": []}),
        encoding="utf-8",
    )
    return work_id


def test_plan_work_item_records_model_invocation_when_session_id_set(
    tmp_path: Path,
) -> None:
    """With ``session_id`` set + a configured planner, the planning step
    records a row in ``model_invocations`` keyed to the session and writes
    a ``PLANNER-NOTE.md`` next to the canonical TEST-PLAN.md."""
    conn, paths, events, repo = _runtime(tmp_path)
    try:
        fake = tmp_path / "bin" / "fake-claude"
        _write_executable(fake, f"#!/usr/bin/env bash\nprintf '{_FAKE_PLANNER_STDOUT}'\n")
        os.environ["PATH"] = str(fake.parent) + os.pathsep + os.environ["PATH"]
        try:
            _install_planner_config(repo, fake)
            work_id = _seed_work_item(conn, paths)

            result = plan_work_item(
                conn, paths, events, work_item_id=work_id, session_id="sess-308"
            )
        finally:
            os.environ["PATH"] = os.environ["PATH"].replace(
                str(fake.parent) + os.pathsep, ""
            )

        assert result["error"] is None

        row = conn.execute(
            "SELECT session_id, task_id, work_item_id, model_role, provider, "
            "tokens_in, tokens_out, cost_usd, exit_code "
            "FROM model_invocations WHERE session_id=?;",
            ("sess-308",),
        ).fetchone()
        assert row is not None, "plan_work_item must record an invocation row"
        assert row["session_id"] == "sess-308"
        # Issue #339 — autonomous-pipeline rows now carry an explicit
        # ``work_item_id`` FK to ``work_items``; ``task_id`` stays NULL
        # (FK to ``tasks(id)`` — different schema slice).
        assert row["task_id"] is None
        assert row["work_item_id"] == work_id
        assert row["exit_code"] == 0
        assert row["tokens_in"] > 0, "tokens_in must be set from prompt estimate"
        assert row["tokens_out"] > 0, "tokens_out must be set from stdout estimate"
        assert row["cost_usd"] > 0, "non-zero provider rates must flow into cost"

        note = paths.runtime_root / "plans" / work_id / "PLANNER-NOTE.md"
        assert note.exists(), "model stdout must be persisted next to TEST-PLAN.md"
        assert "planner-note" in note.read_text(encoding="utf-8")
    finally:
        conn.close()


def test_plan_work_item_without_session_id_records_nothing(tmp_path: Path) -> None:
    """The CLI / dashboard one-shot path must keep the historic behaviour:
    no model row, no extra event, no side-car file."""
    conn, paths, events, repo = _runtime(tmp_path)
    try:
        fake = tmp_path / "bin" / "fake-claude"
        _write_executable(fake, f"#!/usr/bin/env bash\nprintf '{_FAKE_PLANNER_STDOUT}'\n")
        os.environ["PATH"] = str(fake.parent) + os.pathsep + os.environ["PATH"]
        try:
            _install_planner_config(repo, fake)
            work_id = _seed_work_item(conn, paths)

            plan_work_item(conn, paths, events, work_item_id=work_id)
        finally:
            os.environ["PATH"] = os.environ["PATH"].replace(
                str(fake.parent) + os.pathsep, ""
            )

        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM model_invocations;"
        ).fetchone()
        assert rows["n"] == 0, "CLI path (session_id=None) must not record rows"
        assert not (paths.runtime_root / "plans" / work_id / "PLANNER-NOTE.md").exists()
    finally:
        conn.close()


def test_budget_status_reflects_session_cost_after_autonomous_planning(
    tmp_path: Path,
) -> None:
    """Acceptance — ``budget_status`` returns non-zero session cost for a
    session that drove the planning step."""
    conn, paths, events, repo = _runtime(tmp_path)
    try:
        fake = tmp_path / "bin" / "fake-claude"
        _write_executable(fake, f"#!/usr/bin/env bash\nprintf '{_FAKE_PLANNER_STDOUT}'\n")
        os.environ["PATH"] = str(fake.parent) + os.pathsep + os.environ["PATH"]
        try:
            _install_planner_config(repo, fake)
            work_id = _seed_work_item(conn, paths)
            plan_work_item(
                conn, paths, events, work_item_id=work_id, session_id="sess-308-budget"
            )
        finally:
            os.environ["PATH"] = os.environ["PATH"].replace(
                str(fake.parent) + os.pathsep, ""
            )

        status = budget_status(conn, budgets={}, session_id="sess-308-budget")
        assert status["session"]["tokens"] > 0
        assert status["session"]["cost_usd"] > 0
    finally:
        conn.close()


def test_autonomy_step_passes_session_id_to_planner() -> None:
    """``_autonomy_step`` must thread ``session.session_id`` into pipeline
    builders that accept the kwarg. Pipeline builders without it (e.g.
    ``implement_tests_for_work_item``) must keep their historic signature."""
    captured: dict = {}

    def fake_planner(conn, paths, events, *, work_item_id, session_id=None):
        captured["work_item_id"] = work_item_id
        captured["session_id"] = session_id
        return {"work_item_id": work_item_id, "status": "planned", "error": None}

    def fake_implementer(conn, paths, events, *, work_item_id):
        captured["implement_seen_kwargs"] = sorted(
            inspect.signature(fake_implementer).parameters.keys()
        )
        return {"work_item_id": work_item_id, "status": "implemented", "error": None}

    session = _SessionState(
        session_id="sess-thread-308",
        started_at="2026-05-28T00:00:00Z",
        expected_finish_at="2026-05-28T00:01:00Z",
        max_minutes=1,
        preflight={"ok": True, "checks": []},
    )

    assert _autonomy_step(session, None, None, None, "wi-1", "plan", fake_planner)
    assert captured["session_id"] == "sess-thread-308"

    assert _autonomy_step(session, None, None, None, "wi-1", "implement", fake_implementer)
    # The implementer signature must not gain a ``session_id`` kwarg by
    # accident — the introspection branch only adds it when present.
    assert "session_id" not in captured["implement_seen_kwargs"]
