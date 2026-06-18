"""Issue #290 (2nd child) — per-phase checkpoint + resume.

The autonomy loop's per-pending-item block used to run every phase
unconditionally each iteration: ``analyze`` → ``plan`` → ``implement`` →
review-gate → run-tests → final-gate. ``_autonomy_step`` returned ``None``,
so the loop could not tell a phase had failed; downstream phases ran after an
upstream failure and the next iteration restarted at ``analyze``.

Two knobs fix this:

* **Knob 1** — ``_autonomy_step`` returns ``ok`` (bool). The early pipeline
  (analyze/plan/implement) ``break``s on failure so downstream phases do not
  run after an upstream failure.
* **Knob 2** — ``_phase_done(conn, work_id, phase)`` gates each early phase on
  the presence of its proof artifact (``analysis`` / ``test_plan`` / ``patch``).
  On resume, completed phases are skipped and only the missing/failed one runs.

These tests drive the real ``_run_loop`` (the harness mirrors test 6 in
``tests/test_task_synthesis.py``) and patch the SOURCE phase modules, because
``_run_loop`` imports the phase functions locally
(``from .analysis import analyze_work_item`` etc.).
"""
from __future__ import annotations

import threading
import time as _time
from pathlib import Path

import pytest

import agentic_os.analysis as analysis_mod
import agentic_os.autonomy as autonomy
import agentic_os.patch_builder as patch_builder_mod
import agentic_os.test_planning as test_planning_mod
from agentic_os.orchestrator import open_runtime
from agentic_os.paths import RuntimePaths
from agentic_os.work_items import (
    create_work_item_from_payload,
    register_work_item_artifact,
    update_work_item_status,
)


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


def _make_queued_item(repo_root: Path) -> str:
    """Create one queued work item, return its id. Closes its own conn."""
    conn, paths, events, _o = open_runtime(repo_root)
    try:
        detail = create_work_item_from_payload(
            conn,
            paths,
            events,
            {"title": "checkpoint resume probe", "priority": "P2"},
        )
        return detail["work_item"]["id"]
    finally:
        conn.close()


class _Spy:
    """A callable phase stub that counts calls and returns a healthy dict.

    Healthy return shape ``{"status": "planned"}`` reads as ok=True through
    ``_interpret_step_result`` (mirrors the real phase return contract; see
    ``test_interpret_status_analyzing_is_success``). An optional ``on_call``
    hook lets a test register the phase's proof artifact / raise / mutate.
    """

    def __init__(self, *, raises: Exception | None = None, on_call=None):
        self.count = 0
        self._raises = raises
        self._on_call = on_call

    def __call__(self, conn, paths, events, *, work_item_id):
        self.count += 1
        if self._on_call is not None:
            self._on_call(conn, paths, events, work_item_id)
        if self._raises is not None:
            raise self._raises
        return {"status": "planned"}


def _start_loop(repo_root: Path):
    paths = RuntimePaths(
        repo_root=repo_root, runtime_root=repo_root / "agentic-os-runtime"
    )
    paths.ensure()
    session = autonomy._SessionState(
        session_id="checkpoint-loop",
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
    return t, session, stop


def _wait_for_step(session, predicate, *, timeout: float = 5.0) -> bool:
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        for e in list(session.events_log):
            if predicate(e):
                return True
        _time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# 1. Knob 1 — a failed phase breaks the sequence (implement not reached)
# ---------------------------------------------------------------------------


def test_failure_breaks_sequence_before_implement(repo_root: Path, monkeypatch) -> None:
    _make_queued_item(repo_root)

    analyze_spy = _Spy()
    plan_spy = _Spy(raises=RuntimeError("plan kaboom"))
    implement_spy = _Spy()

    monkeypatch.setattr(analysis_mod, "analyze_work_item", analyze_spy)
    monkeypatch.setattr(test_planning_mod, "plan_work_item", plan_spy)
    monkeypatch.setattr(
        patch_builder_mod, "implement_tests_for_work_item", implement_spy
    )

    t, session, stop = _start_loop(repo_root)
    try:
        # Stop as soon as the plan failure lands, so implement (if it were
        # going to be called in the same iteration) has already had its turn.
        seen = _wait_for_step(
            session,
            lambda e: e["step"].startswith("plan:") and e["ok"] is False,
        )
    finally:
        stop.set()
        t.join(timeout=3)

    assert seen, "loop never recorded a failing plan step"
    assert analyze_spy.count >= 1, "analyze should have run before plan"
    assert (
        implement_spy.count == 0
    ), "implement ran after upstream plan failure — sequence was not broken"
    assert not t.is_alive()


# ---------------------------------------------------------------------------
# 2. Knob 2 — resume at the failed phase (analyze artifact present → skipped)
# ---------------------------------------------------------------------------


def test_resume_skips_completed_phase_runs_next(repo_root: Path, monkeypatch) -> None:
    work_id = _make_queued_item(repo_root)

    # Seed ONLY the `analysis` artifact — analyze is "done", plan is the
    # next missing phase.
    conn, paths, events, _o = open_runtime(repo_root)
    try:
        register_work_item_artifact(
            conn,
            paths,
            events,
            work_item_id=work_id,
            kind="analysis",
            path="reports/analysis.json",
        )
    finally:
        conn.close()

    analyze_spy = _Spy()
    plan_spy = _Spy()
    implement_spy = _Spy()

    monkeypatch.setattr(analysis_mod, "analyze_work_item", analyze_spy)
    monkeypatch.setattr(test_planning_mod, "plan_work_item", plan_spy)
    monkeypatch.setattr(
        patch_builder_mod, "implement_tests_for_work_item", implement_spy
    )

    t, session, stop = _start_loop(repo_root)
    try:
        seen = _wait_for_step(session, lambda e: e["step"].startswith("plan:"))
    finally:
        stop.set()
        t.join(timeout=3)

    assert seen, "loop never reached the plan phase"
    assert analyze_spy.count == 0, "analyze ran despite an `analysis` artifact present"
    assert plan_spy.count >= 1, "plan (the resume phase) did not run"
    assert not t.is_alive()


# ---------------------------------------------------------------------------
# 3. Regression — full healthy sequence runs all phases in order
# ---------------------------------------------------------------------------


def test_full_sequence_runs_all_phases(repo_root: Path, monkeypatch) -> None:
    work_id = _make_queued_item(repo_root)

    def _register(kind: str, rel: str):
        def hook(conn, paths, events, wid):
            register_work_item_artifact(
                conn, paths, events, work_item_id=wid, kind=kind, path=rel
            )

        return hook

    analyze_spy = _Spy(on_call=_register("analysis", "reports/analysis.json"))
    plan_spy = _Spy(on_call=_register("test_plan", "reports/test_plan.json"))
    # implement MUST register a `patch` artifact or _should_continue_to_review
    # halts the loop at implement (it checks for a patch artifact).
    implement_spy = _Spy(on_call=_register("patch", "reports/patch.diff"))

    monkeypatch.setattr(analysis_mod, "analyze_work_item", analyze_spy)
    monkeypatch.setattr(test_planning_mod, "plan_work_item", plan_spy)
    monkeypatch.setattr(
        patch_builder_mod, "implement_tests_for_work_item", implement_spy
    )

    review_spy = _Spy()
    run_tests_spy = _Spy()
    final_gate_spy = _Spy()
    monkeypatch.setattr(autonomy, "_autonomy_review_then_apply", review_spy)
    monkeypatch.setattr(autonomy, "_autonomy_run_tests", run_tests_spy)
    monkeypatch.setattr(autonomy, "_autonomy_final_gate", final_gate_spy)

    t, session, stop = _start_loop(repo_root)
    try:
        seen = _wait_for_step(
            session, lambda e: e["step"].startswith("final-gate:")
        )
    finally:
        stop.set()
        t.join(timeout=3)

    assert seen, "loop never reached final-gate — sequence stalled"
    # All six phases ran at least once, in order.
    assert analyze_spy.count >= 1
    assert plan_spy.count >= 1
    assert implement_spy.count >= 1
    assert review_spy.count >= 1
    assert run_tests_spy.count >= 1
    assert final_gate_spy.count >= 1
    # No early phase ran AGAIN after its artifact existed within the same pass
    # before reaching final-gate (gating skips completed phases on re-entry).
    assert analyze_spy.count == 1, "analyze re-ran despite its artifact existing"
    assert plan_spy.count == 1, "plan re-ran despite its artifact existing"
    assert implement_spy.count == 1, "implement re-ran despite its patch existing"
    assert not t.is_alive()


# ---------------------------------------------------------------------------
# 4. _should_continue_to_review preserved — a blocked item halts at implement
# ---------------------------------------------------------------------------


def test_blocked_item_halts_at_review_checkpoint(repo_root: Path, monkeypatch) -> None:
    work_id = _make_queued_item(repo_root)

    def _register(kind: str, rel: str):
        def hook(conn, paths, events, wid):
            register_work_item_artifact(
                conn, paths, events, work_item_id=wid, kind=kind, path=rel
            )

        return hook

    analyze_spy = _Spy(on_call=_register("analysis", "reports/analysis.json"))
    plan_spy = _Spy(on_call=_register("test_plan", "reports/test_plan.json"))

    def _implement_blocks(conn, paths, events, wid):
        # Register a `patch` artifact so the artifact gate marks implement done,
        # but flip the work item to `blocked`. _should_continue_to_review must
        # still halt the loop at the review checkpoint regardless of artifacts.
        register_work_item_artifact(
            conn, paths, events, work_item_id=wid, kind="patch", path="reports/p.diff"
        )
        update_work_item_status(
            conn, events, work_item_id=wid, status="blocked"
        )

    implement_spy = _Spy(on_call=_implement_blocks)

    monkeypatch.setattr(analysis_mod, "analyze_work_item", analyze_spy)
    monkeypatch.setattr(test_planning_mod, "plan_work_item", plan_spy)
    monkeypatch.setattr(
        patch_builder_mod, "implement_tests_for_work_item", implement_spy
    )

    review_spy = _Spy()
    monkeypatch.setattr(autonomy, "_autonomy_review_then_apply", review_spy)

    t, session, stop = _start_loop(repo_root)
    try:
        seen = _wait_for_step(
            session,
            lambda e: e["step"].endswith(":awaiting_operator_decision"),
        )
    finally:
        stop.set()
        t.join(timeout=3)

    assert seen, "blocked item did not produce an awaiting_operator_decision event"
    assert review_spy.count == 0, "review-gate ran despite the blocked checkpoint"
    assert not t.is_alive()
