"""Result parsing, classification, bug rendering, and bug id allocation."""
from __future__ import annotations

import json

import pytest

from agentic_os.results import (
    BugReport,
    Classification,
    TestResult,
    classify_results,
    next_bug_id,
    parse_cucumber_json,
    parse_junit_xml,
    parse_playwright_json,
    render_bug_markdown,
    summarize_classifications,
)


_JUNIT_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="orders" tests="3" failures="1" errors="0" skipped="1">
    <testcase classname="api" name="POST orders rejects negative quantity @bug-001 @functional-orders" time="0.012">
      <failure message="Expected status 400 but got 201" type="AssertionError">stacktrace ...</failure>
    </testcase>
    <testcase classname="api" name="GET orders lists items" time="0.005"/>
    <testcase classname="api" name="DELETE orders not implemented" time="0.001">
      <skipped/>
    </testcase>
  </testsuite>
</testsuites>
"""


_PLAYWRIGHT_JSON = json.dumps({
    "suites": [
        {
            "title": "orders.spec.ts",
            "specs": [
                {
                    "title": "C-001 — Reject negative quantity",
                    "tests": [
                        {
                            "results": [
                                {"status": "failed", "duration": 123, "errors": [{"message": "Expected status 400 but got 201"}]},
                            ]
                        }
                    ],
                },
                {
                    "title": "C-002 — Happy path",
                    "tests": [{"results": [{"status": "passed", "duration": 50}]}],
                },
            ],
            "suites": [],
        }
    ]
}).encode("utf-8")


_CUCUMBER_JSON = json.dumps([
    {
        "uri": "features/orders.feature",
        "name": "Orders",
        "elements": [
            {
                "name": "Reject negative quantity",
                "tags": [{"name": "@functional-orders"}, {"name": "@regression"}],
                "steps": [
                    {"result": {"status": "passed", "duration": 1_000_000}},
                    {"result": {"status": "failed", "duration": 500_000, "error_message": "Expected 400"}},
                ],
            }
        ],
    }
]).encode("utf-8")


def test_parse_junit_extracts_status_and_message() -> None:
    results = parse_junit_xml(_JUNIT_XML)
    by_name = {r.name: r for r in results}
    failed = next(r for r in results if r.status == "failed")
    assert failed.failure_message and "Expected status 400" in failed.failure_message
    assert "@bug-001" in failed.tags
    assert any(r.status == "skipped" for r in results)
    assert any(r.status == "passed" for r in results)


def test_parse_playwright_extracts_failure_message() -> None:
    results = parse_playwright_json(_PLAYWRIGHT_JSON)
    failed = [r for r in results if r.status == "failed"]
    assert len(failed) == 1
    assert "Expected status 400" in (failed[0].failure_message or "")
    assert failed[0].runner == "playwright"


def test_parse_cucumber_extracts_first_failed_step() -> None:
    results = parse_cucumber_json(_CUCUMBER_JSON)
    assert len(results) == 1
    assert results[0].status == "failed"
    assert "@functional-orders" in results[0].tags


def test_classify_known_bug_red_when_tag_matches_allowlist() -> None:
    results = parse_junit_xml(_JUNIT_XML)
    failing = [r for r in results if r.status == "failed"][0]
    classified = classify_results([failing], known_bug_ids=["@bug-001"])
    assert classified[0].category == "known_bug_red"
    assert classified[0].bug_id == "@bug-001"


def test_classify_product_bug_when_no_known_tag() -> None:
    res = TestResult(
        name="C-001 — Reject negative quantity",
        suite="orders",
        status="failed",
        failure_message="Expected status 400 but got 201",
        tags=["@functional-orders"],
        runner="playwright",
    )
    classified = classify_results([res])
    assert classified[0].category == "product_bug"


def test_classify_infra_on_connection_refused() -> None:
    res = TestResult(
        name="C-X",
        suite="orders",
        status="failed",
        failure_message="connect ECONNREFUSED 127.0.0.1:3000",
        runner="playwright",
    )
    classified = classify_results([res])
    assert classified[0].category == "infra"


def test_classify_flaky_when_listed() -> None:
    res = TestResult(name="flake", suite="x", status="failed", failure_message="boom")
    classified = classify_results([res], flaky_names=["flake"])
    assert classified[0].category == "flaky"


def test_classify_test_bug_on_runner_error() -> None:
    res = TestResult(
        name="C-err",
        suite="orders",
        status="error",
        failure_message="SyntaxError: unexpected token",
    )
    classified = classify_results([res])
    assert classified[0].category == "test_bug"


def test_render_bug_markdown_has_required_sections() -> None:
    res = TestResult(
        name="C-001 — Reject negative quantity",
        suite="orders",
        status="failed",
        failure_message="Expected 400 got 201",
        tags=["@functional-orders"],
        runner="playwright",
    )
    bug = render_bug_markdown(
        bug_id="BUG-001",
        title="Negative quantity accepted",
        severity="P1",
        test_result=res,
        expected="POST /orders with quantity=-1 must return 400",
        actual="API returned 201 with the negative quantity persisted",
        evidence_paths=[".agentic-os/evidence/run-1/screenshot.png"],
        repro_steps=[
            "POST /orders with {\"quantity\": -1}",
            "Observe response.status === 201",
        ],
    )
    for section in ("# BUG-001", "## Expected", "## Actual", "## Evidence", "## Repro"):
        assert section in bug.body
    assert isinstance(bug, BugReport)


def test_next_bug_id_increments_max() -> None:
    assert next_bug_id([]) == "BUG-001"
    assert next_bug_id(["BUG-001", "BUG-003", "BUG-007"]) == "BUG-008"
    # Non-numeric ids are ignored.
    assert next_bug_id(["BUG-NOT", "BUG-005"]) == "BUG-006"


def test_summarize_classifications_counts_categories() -> None:
    classified = [
        Classification("a", "s", "pass", "ok"),
        Classification("b", "s", "product_bug", "x"),
        Classification("c", "s", "known_bug_red", "x", bug_id="@bug-001"),
        Classification("d", "s", "infra", "x"),
        Classification("e", "s", "flaky", "x"),
    ]
    summary = summarize_classifications(classified)
    assert summary["pass"] == 1
    assert summary["product_bug"] == 1
    assert summary["known_bug_red"] == 1
    assert summary["infra"] == 1
    assert summary["flaky"] == 1


# ---------------------------------------------------------------------------
# Issue #287 — flaky producer: category oscillation across triage runs.
# ---------------------------------------------------------------------------


def _triage_runtime(tmp_path):
    from agentic_os.events import EventLog
    from agentic_os.orchestrator import CURRENT_PHASE_ID, Orchestrator
    from agentic_os.paths import RuntimePaths
    from agentic_os.storage import init_db

    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    # triage_reports writes events bound to run_id_str; seed that run so the
    # events FK to runs is satisfied.
    task_id = orch.create_task(
        phase_id=CURRENT_PHASE_ID, kind="review", payload={"workflow": "test"}
    )
    orch.lease_task(task_id, owner="orchestrator", ttl_seconds=60)
    orch.mark_running(task_id, owner="orchestrator")
    orch.record_run(
        task_id=task_id,
        run_id="run-2",
        command=["agentic-os", "test"],
        cwd=".",
        env_hash="x",
        log_path=".agentic-os/logs/x.log",
        started_at="2026-01-01T00:00:00Z",
    )
    return conn, paths, events


def _write_last_run(paths, *, scenario, feature_uri, error_message, tags=None):
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
                        "scenario": scenario,
                        "feature_uri": feature_uri,
                        "error_message": error_message,
                        "tags": tags or [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (reports / "summary.md").write_text("# stub\n", encoding="utf-8")


def _flaky_rows(conn, subject):
    return conn.execute(
        "SELECT subject, payload FROM learnings WHERE kind='flaky' AND subject=?;",
        (subject,),
    ).fetchall()


def test_scenario_oscillating_categories_is_recorded_flaky(tmp_path):
    """A scenario classified product_bug last run, now non-product (infra) →
    oscillation → recorded as flaky (no steady product bug)."""
    from agentic_os.workflows import triage_reports

    conn, paths, events = _triage_runtime(tmp_path)
    subject = "features/orders.feature::Reject negative quantity"
    try:
        # Prior run: the same subject was a product_bug.
        events.write(
            "triage.scenario_classified",
            payload={"subject": subject, "category": "product_bug"},
        )

        # This run: the same scenario fails with an infra signal (non-product).
        _write_last_run(
            paths,
            scenario="Reject negative quantity",
            feature_uri="features/orders.feature",
            error_message="connection refused",
        )
        result = triage_reports(paths, events, run_id_str="run-2", auto_file_bugs=False)
        assert result["available"] is True
        rows = _flaky_rows(conn, subject)
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload"])
        # The clustered payload notes the categories that oscillated.
        assert payload.get("category") == "infra"
        assert payload.get("prior_category") == "product_bug"
    finally:
        conn.close()


def test_steady_product_bug_is_not_flaky(tmp_path):
    """A scenario that is a product_bug both runs is steady, not flaky."""
    from agentic_os.workflows import triage_reports

    conn, paths, events = _triage_runtime(tmp_path)
    subject = "features/orders.feature::Reject negative quantity"
    try:
        events.write(
            "triage.scenario_classified",
            payload={"subject": subject, "category": "product_bug"},
        )
        _write_last_run(
            paths,
            scenario="Reject negative quantity",
            feature_uri="features/orders.feature",
            error_message="Expected status 400 but got 201",
        )
        result = triage_reports(paths, events, run_id_str="run-2", auto_file_bugs=False)
        assert any(i["category"] == "product_bug" for i in result["items"])
        assert _flaky_rows(conn, subject) == []
    finally:
        conn.close()
