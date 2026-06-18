"""Golden eval for the deterministic candidate generator (issue #140).

Existing skill tests only assert phrase-level invariants on the planner /
implementer / reviewer prompt files. They don't catch the regression class
the issue is about: the candidate generator silently shrinking to one or
two smoke tests against a realistic spec.

This test points the analysis pipeline at a non-trivial OpenAPI surface
(auth + users CRUD, 9 operations across 6 unique paths) and asserts the
heuristic produces sensible breadth and depth — enough candidates,
coverage of every spec path, and a mix of negative/positive shapes. The
fixture lives under ``tests/fixtures/skill_eval_golden/`` and is checked
in as the golden input.

The pipeline is fully deterministic (no model call), so the assertions
are exact thresholds, not statistical bounds. Bumping the generator's
output should bump the thresholds here in lockstep.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agentic_os.analysis import analyze_work_item
from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db
from agentic_os.storage.db import connect
from agentic_os.work_items import create_work_item_from_payload


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "skill_eval_golden"
SPEC_PATHS = {
    "/auth/login",
    "/auth/refresh",
    "/auth/logout",
    "/users",
    "/users/{id}",
    "/users/{id}/password-reset",
}


def _write_config(repo: Path) -> None:
    """Minimal config that points the analyzer at the golden OpenAPI spec.

    The analyzer reads ``sut.openapi.sources`` and resolves each entry
    against ``repo_root``; we copy the fixture spec into the workspace
    and reference it by relative path.
    """
    cfg = repo / "config" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        """
runtime:
  root: .agentic-os
  timezone: Europe/Warsaw
  max_parallel_tasks: 4
  heartbeat_seconds: 20
  lease_ttl_seconds: 60
  stale_lease_seconds: 90
  shutdown_grace_seconds: 5
  timeouts:
    default_seconds: 1800
    docker_seconds: 240
    test_seconds: 3600
    model_seconds: 1800
    report_seconds: 300
sut:
  root: .
  compose_file: docker-compose.yml
  compose_project_name: agentic-os-sut
  autostart: false
  healthcheck:
    command: ["true"]
    timeout_seconds: 30
    retries: 1
  test_runner: ./run-tests.sh
  install_shim_allowed: false
  openapi:
    sources:
      - type: file
        value: openapi.yaml
models:
  planner: { provider: claude, command: ["claude"], role: opus }
  implementer: { provider: claude, command: ["claude"], role: sonnet }
  reviewer: { provider: codex, command: ["codex"], role: codex }
dashboard:
  host: 127.0.0.1
  port: 8765
  enable_write_endpoints: false
paths:
  reports: reports
  bugs: bugs
  evidence: evidence
  prompts: .qualitycat/prompts
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
""".lstrip(),
        encoding="utf-8",
    )


@pytest.fixture
def golden_workspace(tmp_path: Path) -> RuntimePaths:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    _write_config(repo)
    shutil.copy2(FIXTURE / "openapi.yaml", repo / "openapi.yaml")
    conn = init_db(paths.db)
    orch = Orchestrator(conn, paths, EventLog(conn, paths))
    orch.seed_phases()
    conn.close()
    return paths


def _seed_work_item(paths: RuntimePaths) -> str:
    spec_body = (FIXTURE / "pretask.md").read_text(encoding="utf-8")
    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        detail = create_work_item_from_payload(
            conn,
            paths,
            events,
            {
                "title": "Identity API coverage",
                "priority": "P1",
                "business_goal": "Cover auth + users CRUD with happy + unhappy paths.",
                "expected_behavior": spec_body,
                "relevant_surfaces": ", ".join(sorted(SPEC_PATHS)),
            },
            default_sut_root=".",
        )
        return detail["work_item"]["id"]
    finally:
        conn.close()


def test_candidate_generator_produces_breadth_and_depth(golden_workspace: RuntimePaths) -> None:
    paths = golden_workspace
    work_item_id = _seed_work_item(paths)
    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        analyze_work_item(conn, paths, events, work_item_id=work_item_id)
    finally:
        conn.close()

    candidates_path = paths.runtime_root / "analysis" / work_item_id / "candidate-tests.json"
    payload = json.loads(candidates_path.read_text(encoding="utf-8"))
    items = payload.get("candidates") or payload.get("items") or []

    api_items = [it for it in items if it.get("bucket") == "API"]
    ui_items = [it for it in items if it.get("bucket") == "UI"]

    # --- count gate -----------------------------------------------------
    # 9 spec operations × generator's 1+ candidates per op should clear
    # the issue's "8-12 sensible candidates, not 1-2 smoke" bar. We
    # observe 16 today (9 API + 6 UI + 1 Security); 10 is the floor.
    assert len(items) >= 10, (
        f"candidate generator only emitted {len(items)} candidates; "
        f"regression vs the 8-12 floor in issue #140"
    )

    # --- bucket diversity gate -----------------------------------------
    # The issue calls for ≥3 categories of problem types. The generator
    # encodes them as buckets (API / UI / Security).
    buckets = {it.get("bucket") for it in items if it.get("bucket")}
    assert buckets >= {"API", "UI"}, (
        f"candidate generator dropped a coverage bucket; got buckets={sorted(buckets)}"
    )
    assert len(buckets) >= 3, (
        f"need ≥3 problem-type buckets, got {sorted(buckets)}"
    )

    # --- API path coverage gate ----------------------------------------
    # Every spec path must show up in at least one API candidate. The
    # most common regression class is the generator silently dropping a
    # resource — this catches it.
    api_paths = {it.get("target_path") for it in api_items if it.get("target_path")}
    missing = SPEC_PATHS - api_paths
    assert not missing, (
        f"API candidates miss spec paths: {sorted(missing)}; "
        f"covered={sorted(api_paths)}"
    )

    # --- HTTP method diversity gate ------------------------------------
    methods = {it.get("target_method") for it in api_items if it.get("target_method")}
    assert methods >= {"GET", "POST", "PUT", "DELETE"}, (
        f"API candidates dropped HTTP verbs; got methods={sorted(methods)}"
    )

    # --- positive/negative split ---------------------------------------
    negative = [it for it in items if it.get("negative_or_boundary")]
    happy = [it for it in items if not it.get("negative_or_boundary")]
    assert len(negative) >= 3, f"negative/boundary candidates: {len(negative)} < 3"
    assert len(happy) >= 3, f"happy-path candidates: {len(happy)} < 3"

    # --- UI candidates cover the spec paths too ------------------------
    ui_paths = {it.get("target_page") for it in ui_items if it.get("target_page")}
    assert ui_paths >= SPEC_PATHS, (
        f"UI candidates miss spec paths: {sorted(SPEC_PATHS - ui_paths)}; "
        f"covered={sorted(ui_paths)}"
    )

    # --- candidate schema gate -----------------------------------------
    # Every candidate must carry the downstream fields the planner /
    # reviewer / dashboard depend on. Required keys vary by bucket: API
    # candidates need method+path, UI candidates need target_page, and
    # everything needs the core identifying fields.
    base_required = {
        "candidate_id",
        "bucket",
        "decision",
        "test_type",
        "expected_assertion",
        "priority",
    }
    for it in items:
        missing_keys = base_required - set(it.keys())
        assert not missing_keys, (
            f"candidate {it.get('candidate_id')} missing keys: {sorted(missing_keys)}"
        )
        assert it["decision"] == "needs_operator_decision", (
            f"deterministic generator must emit drafts only; "
            f"candidate {it['candidate_id']} has decision={it['decision']!r}"
        )
        if it.get("bucket") == "API":
            assert it.get("target_method"), (
                f"API candidate {it['candidate_id']} missing target_method"
            )
            assert it.get("target_path"), (
                f"API candidate {it['candidate_id']} missing target_path"
            )
        elif it.get("bucket") == "UI":
            assert it.get("target_page"), (
                f"UI candidate {it['candidate_id']} missing target_page"
            )
