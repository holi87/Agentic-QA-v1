"""Issue #290 — opt-in bounded task synthesis from real signals.

The autonomy loop, when the queue is empty and ``autonomy.task_synthesis``
is on, synthesizes work items from two narrow signals:

* failing tests in ``reports/last-run.json``;
* ``coverage_gap`` learnings for the active project.

Synthesized items enter the normal pipeline (advisory discipline: gates
still decide). These tests pin the discriminating behaviours: the cap is
respected across BOTH sources combined, the ``source_signal`` lands in the
persisted spec, dedup against still-open items prevents synthesis loops,
known-bug/flaky failures are skipped, and the flag defaults OFF.
"""
from __future__ import annotations

import json
import threading
import time as _time
from pathlib import Path

import pytest

from agentic_os import task_synthesis
from agentic_os.learnings import record_learning
from agentic_os.orchestrator import CURRENT_PHASE_ID, open_runtime
from agentic_os.projects import DEFAULT_PROJECT_ID
from agentic_os.work_items import list_work_items, read_work_item_spec


_MINIMAL_CONFIG = """\
runtime:
  root: agentic-os-runtime
  timezone: Europe/Warsaw
  max_parallel_tasks: 1
  heartbeat_seconds: 10
  lease_ttl_seconds: 600
  stale_lease_seconds: 1800
  shutdown_grace_seconds: 30
  timeouts:
    default_seconds: 600
    docker_seconds: 120
    test_seconds: 900
    model_seconds: 600
    report_seconds: 120

sut:
  root: .
  compose_file: docker-compose.yml
  compose_project_name: app
  autostart: false
  healthcheck:
    command: ["sh", "-c", "exit 0"]
    timeout_seconds: 5
    retries: 1
  test_runner: scripts/run-tests.sh
  install_shim_allowed: false

models:
  planner:
    provider: claude
    command: ["claude", "--model", "opus"]
    role: opus
  implementer:
    provider: claude
    command: ["claude", "--model", "sonnet"]
    role: sonnet
  reviewer:
    provider: codex
    command: ["codex"]
    role: codex

budgets:
  fail_mode: warn
  session:
    max_tokens: 1000
    max_usd: 5.0
  per_role:
    planner:
      max_tokens: 500

dashboard:
  host: 127.0.0.1
  port: 8765
  enable_write_endpoints: false

paths:
  reports: reports
  bugs: bugs
  evidence: evidence
  prompts: prompts

reports:
  copy_reports_script: scripts/copy-reports.sh
  extract_last_run_script: scripts/extract-last-run.sh
  build_summary_script: scripts/build-summary.sh
  require_reports_on_failure: true

gates:
  known_bugs_fail_exit: true
  assertion_changes_require_decision: true
  exact_spec_failure_opens_bug: true
  require_functional_area_tag: true
  require_lifecycle_tag: true
  infrastructure_exit_code: 2
"""


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    cfg = repo / "config" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_MINIMAL_CONFIG, encoding="utf-8")
    return repo


def _write_last_run(repo: Path, failures: list[dict]) -> None:
    reports = repo / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "last-run.json").write_text(
        json.dumps(
            {
                "ran_at": "2026-01-01T00:00:00Z",
                "total": len(failures),
                "passed": 0,
                "failed": len(failures),
                "skipped": 0,
                "failures": failures,
            }
        ),
        encoding="utf-8",
    )
    (reports / "summary.md").write_text("# stub\n", encoding="utf-8")


def _failure(scenario: str, *, tags: list[str] | None = None) -> dict:
    return {
        "scenario": scenario,
        "feature_uri": f"features/{scenario.replace(' ', '-')}.feature",
        "error_message": "boom",
        "tags": tags or [],
    }


def _decisions(conn) -> list:
    return conn.execute(
        "SELECT topic, actor, rationale FROM decisions WHERE actor='planner-autopilot';"
    ).fetchall()


def _spec_texts(conn, paths) -> list[str]:
    out = []
    for item in list_work_items(conn, project_id=DEFAULT_PROJECT_ID):
        out.append(read_work_item_spec(paths, item))
    return out


# ---------------------------------------------------------------------------
# 1. cap is respected and source_signal lands in the persisted spec
# ---------------------------------------------------------------------------


def test_synthesizes_up_to_cap_with_signal_in_spec_and_decisions(repo_root: Path) -> None:
    conn, paths, events, _o = open_runtime(repo_root)
    try:
        _write_last_run(
            repo_root,
            [_failure(f"scenario {i}") for i in range(5)],
        )
        created = task_synthesis.synthesize_for_idle(
            conn, paths, events, project_id=DEFAULT_PROJECT_ID, max_items=3
        )
        assert created == 3

        items = list_work_items(conn, project_id=DEFAULT_PROJECT_ID)
        assert len(items) == 3
        for spec in _spec_texts(conn, paths):
            assert "failing-test::" in spec

        # Each synthesized item recorded an autopilot decision (audit trail).
        assert len(_decisions(conn)) == 3
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. dedup — a second pass produces nothing while items are still open
# ---------------------------------------------------------------------------


def test_second_pass_dedups_against_open_items(repo_root: Path) -> None:
    conn, paths, events, _o = open_runtime(repo_root)
    try:
        # 3 failures, cap 3 → all consumed on the first pass so the second
        # pass has only already-open signals to dedup against.
        _write_last_run(repo_root, [_failure(f"scenario {i}") for i in range(3)])
        first = task_synthesis.synthesize_for_idle(
            conn, paths, events, project_id=DEFAULT_PROJECT_ID, max_items=3
        )
        assert first == 3
        # Same signals, items still open (queued) → zero new items.
        second = task_synthesis.synthesize_for_idle(
            conn, paths, events, project_id=DEFAULT_PROJECT_ID, max_items=3
        )
        assert second == 0
        assert len(list_work_items(conn, project_id=DEFAULT_PROJECT_ID)) == 3
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. known-bug / @bug-NNN failures are not synthesized
# ---------------------------------------------------------------------------


def test_known_bug_failure_is_skipped(repo_root: Path) -> None:
    conn, paths, events, _o = open_runtime(repo_root)
    try:
        _write_last_run(
            repo_root,
            [
                _failure("known bug stays red", tags=["@known-bug"]),
                _failure("tagged bug", tags=["@bug-123"]),
            ],
        )
        created = task_synthesis.synthesize_for_idle(
            conn, paths, events, project_id=DEFAULT_PROJECT_ID, max_items=3
        )
        assert created == 0
        assert list_work_items(conn, project_id=DEFAULT_PROJECT_ID) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. coverage_gap learnings synthesize, counting against the SAME cap
# ---------------------------------------------------------------------------


def test_coverage_gap_learning_synthesizes_against_shared_cap(repo_root: Path) -> None:
    conn, paths, events, _o = open_runtime(repo_root)
    try:
        # 2 failing tests + 2 coverage gaps, cap 3 → exactly 3 created total.
        _write_last_run(
            repo_root,
            [_failure("scenario a"), _failure("scenario b")],
        )
        record_learning(
            conn,
            kind="coverage_gap",
            subject="appkey::checkout",
            payload={"category": "checkout"},
            actor="planner-autopilot",
        )
        record_learning(
            conn,
            kind="coverage_gap",
            subject="appkey::search",
            payload={"category": "search"},
            actor="planner-autopilot",
        )
        created = task_synthesis.synthesize_for_idle(
            conn, paths, events, project_id=DEFAULT_PROJECT_ID, max_items=3
        )
        assert created == 3
        specs = _spec_texts(conn, paths)
        # At least one of the created items is a coverage-gap task.
        assert any("coverage-gap::" in s for s in specs)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5. flag OFF → nothing synthesized (default preserved)
# ---------------------------------------------------------------------------


def test_flag_off_synthesizes_nothing_in_loop(repo_root: Path) -> None:
    """With no autonomy.task_synthesis key, the loop helper reports it off."""
    from agentic_os.autonomy import _task_synthesis_enabled
    from agentic_os.config import load_or_default

    cfg = load_or_default(repo_root)
    assert _task_synthesis_enabled(cfg) is False


def test_flag_on_is_recognized(repo_root: Path) -> None:
    from agentic_os.autonomy import _task_synthesis_enabled, _task_synthesis_cap
    from agentic_os.config import load_or_default

    cfg_path = repo_root / "config" / "agentic-os.yml"
    cfg_path.write_text(
        cfg_path.read_text()
        + "\nautonomy:\n  task_synthesis: true\n  task_synthesis_max_per_cycle: 2\n",
        encoding="utf-8",
    )
    cfg = load_or_default(repo_root)
    assert _task_synthesis_enabled(cfg) is True
    assert _task_synthesis_cap(cfg) == 2


# ---------------------------------------------------------------------------
# 6. loop resilience — a raising synthesize_for_idle records + continues
# ---------------------------------------------------------------------------


def test_loop_records_failure_and_continues_when_synthesis_raises(
    repo_root: Path, monkeypatch
) -> None:
    """The empty-queue branch wraps synthesis best-effort: a raised exception
    is recorded as a ``task_synthesis`` failure and the loop keeps running."""
    import agentic_os.autonomy as autonomy

    # Enable synthesis so the loop reaches the call.
    cfg_path = repo_root / "config" / "agentic-os.yml"
    cfg_path.write_text(
        cfg_path.read_text()
        + "\nautonomy:\n  task_synthesis: true\n  task_synthesis_max_per_cycle: 3\n",
        encoding="utf-8",
    )

    def _boom(*a, **k):
        raise RuntimeError("synth kaboom")

    monkeypatch.setattr(autonomy.task_synthesis, "synthesize_for_idle", _boom)

    paths = RuntimePaths(repo_root=repo_root, runtime_root=repo_root / "agentic-os-runtime")
    paths.ensure()

    session = autonomy._SessionState(
        session_id="loop-synth",
        started_at="2026-01-01T00:00:00+00:00",
        expected_finish_at="2026-01-01T00:01:00+00:00",
        max_minutes=1,
        preflight={"ok": True},
    )
    stop = threading.Event()
    pause = threading.Event()

    t = threading.Thread(
        target=autonomy._run_loop, args=(paths, session, stop, pause), daemon=True
    )
    t.start()
    deadline = _time.time() + 5
    while _time.time() < deadline:
        steps = [e["step"] for e in list(session.events_log)]
        if "task_synthesis" in steps:
            break
        _time.sleep(0.02)
    stop.set()
    t.join(timeout=3)

    rows = [e for e in list(session.events_log) if e["step"] == "task_synthesis"]
    assert rows, "loop never recorded a task_synthesis step"
    assert rows[0]["ok"] is False
    assert "kaboom" in rows[0]["detail"]
    assert not t.is_alive()


# ---------------------------------------------------------------------------
# 7. config validation — both new keys accepted; cap min enforced
# ---------------------------------------------------------------------------


def test_config_validates_task_synthesis_keys() -> None:
    import copy

    from agentic_os.config import _validate
    from test_notification_dispatch import _BASE_CONFIG  # type: ignore

    cfg = copy.deepcopy(_BASE_CONFIG)
    cfg["autonomy"] = {"task_synthesis": True, "task_synthesis_max_per_cycle": 3}
    assert _validate(cfg) == []
    # bool key must be a bool
    cfg["autonomy"]["task_synthesis"] = "yes"
    assert any("task_synthesis" in e for e in _validate(cfg))
    # cap must be an int >= 1
    cfg["autonomy"] = {"task_synthesis_max_per_cycle": 0}
    assert any("task_synthesis_max_per_cycle" in e for e in _validate(cfg))


from agentic_os.paths import RuntimePaths  # noqa: E402  (used by test 6)
