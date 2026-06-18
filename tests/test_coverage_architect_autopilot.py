"""Planner coverage architect autopilot decision rule."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from agentic_os.analysis import (
    _apply_coverage_architect,
    _autopilot_decision_rule,
    _coverage_architect_enabled,
)
from agentic_os.paths import RuntimePaths


def _candidate(**overrides) -> Dict[str, Any]:
    base = {
        "candidate_id": "CASE-001",
        "decision": "needs_operator_decision",
        "test_type": "api",
        "target_method": "GET",
        "target_path": "/orders",
        "notes": ["Derived from task spec."],
    }
    base.update(overrides)
    return base


def _paths_with_flag(tmp_path: Path, *, flag: bool) -> RuntimePaths:
    paths = RuntimePaths(repo_root=tmp_path, runtime_root=tmp_path / ".agentic-os")
    paths.ensure()
    cfg_path = tmp_path / "config" / "agentic-os.yml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(_CONFIG_TEMPLATE.format(flag=str(flag).lower()), encoding="utf-8")
    return paths


# --- pure decision rule ----------------------------------------------------

def test_rule_api_get_promotes() -> None:
    assert _autopilot_decision_rule(_candidate()) == "api-read-only:GET"


def test_rule_api_head_promotes() -> None:
    assert _autopilot_decision_rule(
        _candidate(target_method="HEAD", target_path="/orders")
    ) == "api-read-only:HEAD"


def test_rule_api_options_promotes() -> None:
    assert _autopilot_decision_rule(
        _candidate(target_method="OPTIONS", target_path="/orders")
    ) == "api-read-only:OPTIONS"


def test_rule_api_post_blocks() -> None:
    assert _autopilot_decision_rule(
        _candidate(target_method="POST", target_path="/orders")
    ) is None


def test_rule_api_put_blocks() -> None:
    assert _autopilot_decision_rule(
        _candidate(target_method="PUT", target_path="/orders/1")
    ) is None


def test_rule_api_delete_blocks() -> None:
    assert _autopilot_decision_rule(
        _candidate(target_method="DELETE", target_path="/orders/1")
    ) is None


def test_rule_api_without_path_blocks() -> None:
    assert _autopilot_decision_rule(
        _candidate(target_method="GET", target_path=None)
    ) is None


def test_rule_ui_navigational_promotes() -> None:
    assert _autopilot_decision_rule(
        _candidate(test_type="ui", target_page="/orders", target_method=None)
    ) == "ui-navigational:/orders"


def test_rule_ui_form_blocks() -> None:
    for path in ("/orders/new", "/orders/edit", "/login", "/signup", "/register"):
        assert _autopilot_decision_rule(
            _candidate(test_type="ui", target_page=path, target_method=None)
        ) is None


def test_rule_ui_without_target_blocks() -> None:
    assert _autopilot_decision_rule(
        _candidate(test_type="ui", target_page=None, target_method=None)
    ) is None


def test_rule_unknown_test_type_blocks() -> None:
    assert _autopilot_decision_rule(
        _candidate(test_type="accessibility", target_method=None)
    ) is None


# --- apply_coverage_architect ---------------------------------------------

def test_apply_does_nothing_when_flag_off(tmp_path: Path) -> None:
    paths = _paths_with_flag(tmp_path, flag=False)
    payload = {"items": [_candidate(), _candidate(candidate_id="CASE-002")]}
    summary: Dict[str, int] = {}
    _apply_coverage_architect(paths, payload, summary)
    for item in payload["items"]:
        assert item["decision"] == "needs_operator_decision"
        assert "actor" not in item
    assert "planner_autopilot_flipped" not in summary


def test_apply_flips_safe_candidates_when_flag_on(tmp_path: Path) -> None:
    paths = _paths_with_flag(tmp_path, flag=True)
    payload = {
        "items": [
            _candidate(candidate_id="API-GET"),  # promotes
            _candidate(candidate_id="API-POST", target_method="POST"),  # stays
            _candidate(
                candidate_id="UI-LIST",
                test_type="ui",
                target_page="/orders",
                target_method=None,
            ),  # promotes
            _candidate(
                candidate_id="UI-NEW",
                test_type="ui",
                target_page="/orders/new",
                target_method=None,
            ),  # stays — form
        ]
    }
    summary: Dict[str, int] = {}
    _apply_coverage_architect(paths, payload, summary)
    by_id = {i["candidate_id"]: i for i in payload["items"]}
    assert by_id["API-GET"]["decision"] == "generate_now"
    assert by_id["API-GET"]["actor"] == "planner-autopilot"
    assert any("planner-autopilot" in n for n in by_id["API-GET"]["notes"])
    assert by_id["API-POST"]["decision"] == "needs_operator_decision"
    assert "actor" not in by_id["API-POST"]
    assert by_id["UI-LIST"]["decision"] == "generate_now"
    assert by_id["UI-NEW"]["decision"] == "needs_operator_decision"
    assert summary["planner_autopilot_flipped"] == 2


def test_apply_preserves_already_decided_items(tmp_path: Path) -> None:
    paths = _paths_with_flag(tmp_path, flag=True)
    payload = {
        "items": [
            _candidate(candidate_id="OP-APPROVED", decision="generate_now"),
            _candidate(candidate_id="NOT-TESTABLE", decision="not_testable"),
        ]
    }
    summary: Dict[str, int] = {}
    _apply_coverage_architect(paths, payload, summary)
    by_id = {i["candidate_id"]: i for i in payload["items"]}
    # generate_now stays generate_now without gaining an autopilot actor.
    assert by_id["OP-APPROVED"]["decision"] == "generate_now"
    assert "actor" not in by_id["OP-APPROVED"]
    assert by_id["NOT-TESTABLE"]["decision"] == "not_testable"


def test_apply_no_op_when_no_safe_candidates(tmp_path: Path) -> None:
    paths = _paths_with_flag(tmp_path, flag=True)
    payload = {
        "items": [
            _candidate(candidate_id="API-POST", target_method="POST"),
            _candidate(
                candidate_id="UI-FORM",
                test_type="ui",
                target_page="/login",
                target_method=None,
            ),
        ]
    }
    summary: Dict[str, int] = {}
    _apply_coverage_architect(paths, payload, summary)
    assert "planner_autopilot_flipped" not in summary
    assert all(i["decision"] == "needs_operator_decision" for i in payload["items"])


# --- issue #287: coverage_gap producer ------------------------------------


def _conn_for(paths: RuntimePaths):
    from agentic_os.storage import init_db

    return init_db(paths.db)


def _gap_candidate(candidate_id: str, bucket: str, test_type: str) -> Dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "bucket": bucket,
        "test_type": test_type,
        "decision": "needs_operator_decision",
        "notes": [],
    }


def _coverage_gap_rows(conn, category: str):
    return conn.execute(
        "SELECT subject, payload FROM learnings WHERE kind='coverage_gap' "
        "AND subject LIKE ?;",
        (f"%::{category}",),
    ).fetchall()


def test_recurring_in_run_gap_category_records_coverage_gap(tmp_path: Path) -> None:
    """Two candidates of the same gap category in one run = recurring → record."""
    paths = _paths_with_flag(tmp_path, flag=True)
    conn = _conn_for(paths)
    try:
        payload = {
            "items": [
                _gap_candidate("SEC-SPEC-001", "Security", "security"),
                _gap_candidate("SEC-SPEC-002", "Security", "security"),
            ]
        }
        summary: Dict[str, int] = {}
        _apply_coverage_architect(paths, payload, summary, conn=conn)
        rows = _coverage_gap_rows(conn, "security")
        assert len(rows) == 1
    finally:
        conn.close()


def test_single_gap_category_not_recorded_until_recurs(tmp_path: Path) -> None:
    """One lone gap candidate, never seen before, is not yet recurring."""
    paths = _paths_with_flag(tmp_path, flag=True)
    conn = _conn_for(paths)
    try:
        payload = {"items": [_gap_candidate("A11Y-SPEC-001", "Accessibility", "accessibility")]}
        summary: Dict[str, int] = {}
        _apply_coverage_architect(paths, payload, summary, conn=conn)
        assert _coverage_gap_rows(conn, "accessibility") == []
    finally:
        conn.close()


def test_gap_recurs_across_runs_records_coverage_gap(tmp_path: Path) -> None:
    """A gap seen in a prior run (existing learning) recurs → record on re-sight."""
    from agentic_os.learnings import record_learning

    paths = _paths_with_flag(tmp_path, flag=True)
    conn = _conn_for(paths)
    try:
        # Prior run already surfaced this gap for the SUT.
        existing = conn.execute(
            "SELECT subject FROM learnings WHERE kind='coverage_gap';"
        ).fetchall()
        assert existing == []
        payload = {"items": [_gap_candidate("A11Y-SPEC-001", "Accessibility", "accessibility")]}
        # Seed a prior learning for whatever sut_key the producer derives by
        # running once with two candidates to force a record, then re-run with one.
        payload_seed = {
            "items": [
                _gap_candidate("A11Y-SPEC-001", "Accessibility", "accessibility"),
                _gap_candidate("A11Y-SPEC-002", "Accessibility", "accessibility"),
            ]
        }
        _apply_coverage_architect(paths, payload_seed, {}, conn=conn)
        seeded = _coverage_gap_rows(conn, "accessibility")
        assert len(seeded) == 1

        # A later run with a single accessibility gap still records because the
        # subject already exists (recurring across runs).
        _apply_coverage_architect(paths, payload, {}, conn=conn)
        rows = _coverage_gap_rows(conn, "accessibility")
        assert len(rows) == 1  # upsert keeps one row per subject
    finally:
        conn.close()


def test_coverage_gap_producer_does_not_change_promotion(tmp_path: Path) -> None:
    """Recording a gap must not alter candidate promotion behaviour."""
    paths = _paths_with_flag(tmp_path, flag=True)
    conn = _conn_for(paths)
    try:
        payload = {
            "items": [
                _candidate(candidate_id="API-GET"),  # promotes
                _gap_candidate("SEC-SPEC-001", "Security", "security"),
                _gap_candidate("SEC-SPEC-002", "Security", "security"),
            ]
        }
        summary: Dict[str, int] = {}
        _apply_coverage_architect(paths, payload, summary, conn=conn)
        by_id = {i["candidate_id"]: i for i in payload["items"]}
        assert by_id["API-GET"]["decision"] == "generate_now"
        assert summary["planner_autopilot_flipped"] == 1
    finally:
        conn.close()


def test_apply_works_without_conn(tmp_path: Path) -> None:
    """Backward-compatible: omitting conn skips gap recording, no crash."""
    paths = _paths_with_flag(tmp_path, flag=True)
    payload = {
        "items": [
            _gap_candidate("SEC-SPEC-001", "Security", "security"),
            _gap_candidate("SEC-SPEC-002", "Security", "security"),
        ]
    }
    summary: Dict[str, int] = {}
    _apply_coverage_architect(paths, payload, summary)  # no conn
    assert payload["items"][0]["decision"] == "needs_operator_decision"


def test_flag_reader_handles_missing_config(tmp_path: Path) -> None:
    """Config errors must default the flag off (safe)."""
    paths = RuntimePaths(repo_root=tmp_path, runtime_root=tmp_path / ".agentic-os")
    paths.ensure()
    # No config/agentic-os.yml present at all — load_or_default may
    # synthesize defaults; in either case the flag must read False.
    assert _coverage_architect_enabled(paths) is False


_CONFIG_TEMPLATE = """\
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

autonomy:
  coverage_architect: {flag}
"""
