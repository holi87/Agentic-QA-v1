"""Analyze pipeline, TEST-PLAN.json, approval, and v2 generator integration."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from agentic_os.analysis import _build_sut_map, analyze_work_item
from agentic_os.analysis.inputs import _collect_inputs
from agentic_os.autonomy.concurrency import ConcurrencyController
from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.patch_builder import implement_tests_for_work_item
from agentic_os.storage import init_db
from agentic_os.test_planning import (
    plan_work_item,
    read_plan_candidates,
    update_plan_candidate_decision,
)
from agentic_os.work_items import create_work_item_from_payload


_CONFIG_WITH_OPENAPI = """
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
  compose_file: null
  compose_project_name: phase2
  autostart: false
  healthcheck:
    command: ["true"]
    timeout_seconds: 1
    retries: 0
  test_runner: ./run-tests.sh
  install_shim_allowed: false
  kind: web_api
  base_url: http://127.0.0.1:3000
  api_base_url: http://127.0.0.1:3000/api
  openapi:
    sources:
      - type: file
        value: docs/openapi.yaml
  docs:
    sources:
      - type: file
        value: docs/requirements.md
  tests_dir: tests
models:
  planner:
    provider: claude
    command: ["claude"]
    role: opus
  implementer:
    provider: claude
    command: ["claude"]
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
  prompts: config/prompts
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

_OPENAPI_YAML = textwrap.dedent(
    """\
    openapi: 3.0.0
    info: {title: Orders, version: "1.0"}
    paths:
      /orders:
        post:
          operationId: createOrder
          summary: Create order
          responses:
            "201": {description: created}
            "400": {description: bad}
        get:
          operationId: listOrders
          responses:
            "200": {description: ok}
    """
)


def _setup_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    cfg = repo / "config" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_CONFIG_WITH_OPENAPI.lstrip(), encoding="utf-8")
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "openapi.yaml").write_text(_OPENAPI_YAML, encoding="utf-8")
    (repo / "docs" / "requirements.md").write_text(
        "# Requirements\n\n## Validation\n\nQuantity must be positive.\n",
        encoding="utf-8",
    )
    # Make sut.root look like a node project so discovery classifies it.
    (repo / "package.json").write_text('{"name":"sut"}', encoding="utf-8")
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    Orchestrator(conn, paths, events).seed_phases()
    detail = create_work_item_from_payload(
        conn,
        paths,
        events,
        {
            "title": "Pipeline integration task",
            "business_goal": "validate v2 integration",
            "expected_behavior": "spec",
        },
        default_sut_root=".",
    )
    return conn, paths, events, detail["work_item"]["id"]


def test_analyze_emits_v2_inventories(tmp_path: Path) -> None:
    conn, paths, events, work_id = _setup_repo(tmp_path)
    try:
        result = analyze_work_item(conn, paths, events, work_item_id=work_id)
        assert result["status"] == "analyzing"
        sut_map = json.loads(
            (paths.runtime_root / "analysis" / work_id / "sut-map.json").read_text(encoding="utf-8")
        )
        # Phase 2.5 fields present.
        assert "openapi_inventory" in sut_map
        assert sut_map["openapi_inventory"], "expected at least one parsed OpenAPI inventory"
        assert sut_map["openapi_inventory"][0]["operations"][0]["path"] == "/orders"
        assert "docs_inventory" in sut_map
        assert sut_map["docs_inventory"][0]["sections"], "expected docs sections"
        assert sut_map["discovery"] is not None
        assert sut_map["discovery"]["stack"] in ("node", "mixed", "python", "unknown")
    finally:
        conn.close()


# ---- issue #359: planner fan-out of the three SUT-map probes ----------------
# `_build_sut_map` runs the OpenAPI / docs / discovery probes concurrently under
# a ConcurrencyController. The probe set is independent (each writes a separate
# slice of the map), so parallelizing must be byte-equivalent to serial and
# deterministic, and a single probe blowing up must degrade to a recorded gap —
# never unwind the whole analysis.


def _strip_volatile(sut_map: dict) -> dict:
    """Drop inherently per-run timestamps so maps compare by content.

    These fields (the map's own `generated_at` and each ingested doc's
    `ingested_at`) are wall-clock stamps unrelated to probe fan-out; they
    differ run-to-run even serially, so equivalence is asserted on everything
    else.
    """
    out = json.loads(json.dumps(sut_map))  # deep copy
    out.pop("generated_at", None)
    for entry in out.get("docs_inventory") or []:
        if isinstance(entry, dict):
            entry.pop("ingested_at", None)
    return out


def test_sut_map_parallel_probes_equivalent_to_serial(tmp_path: Path) -> None:
    conn, paths, events, work_id = _setup_repo(tmp_path)
    try:
        inputs = _collect_inputs(conn, paths, work_id)
        # cap=1 forces the three probes to run one-at-a-time (serial);
        # cap=4 lets all three overlap. Output must be identical.
        serial = _build_sut_map(
            paths, inputs, controller=ConcurrencyController(global_limit=1)
        )
        parallel = _build_sut_map(
            paths, inputs, controller=ConcurrencyController(global_limit=4)
        )
        assert _strip_volatile(serial) == _strip_volatile(parallel)

        # Anti-vacuous: the fixture must actually drive ALL THREE probes,
        # otherwise the equivalence above proves nothing about fan-out.
        assert parallel["openapi_inventory"][0]["operations"], "openapi probe idle"
        assert parallel["docs_inventory"][0]["sections"], "docs probe idle"
        assert parallel["discovery"] is not None, "discovery probe idle"
        assert parallel["probe_gaps"] == []
    finally:
        conn.close()


def test_sut_map_parallel_probes_are_deterministic(tmp_path: Path) -> None:
    conn, paths, events, work_id = _setup_repo(tmp_path)
    try:
        inputs = _collect_inputs(conn, paths, work_id)
        first = _build_sut_map(
            paths, inputs, controller=ConcurrencyController(global_limit=4)
        )
        second = _build_sut_map(
            paths, inputs, controller=ConcurrencyController(global_limit=4)
        )
        assert _strip_volatile(first) == _strip_volatile(second)
    finally:
        conn.close()


def test_failed_probe_records_gap_and_analysis_still_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, paths, events, work_id = _setup_repo(tmp_path)
    try:
        import agentic_os.sut_discovery as sut_discovery

        def _boom(*_args, **_kwargs):
            raise RuntimeError("discovery exploded")

        monkeypatch.setattr(sut_discovery, "discover_sut", _boom)

        # Must NOT raise — the discovery probe failure is masked into a gap.
        analyze_work_item(conn, paths, events, work_item_id=work_id)

        sut_map = json.loads(
            (paths.runtime_root / "analysis" / work_id / "sut-map.json").read_text(
                encoding="utf-8"
            )
        )
        # Failed probe → its slice is empty and a gap is recorded.
        assert sut_map["discovery"] is None
        assert any(g["probe"] == "discovery" for g in sut_map["probe_gaps"])
        # Surviving probes still produced their slices.
        assert sut_map["openapi_inventory"][0]["operations"]
        assert sut_map["docs_inventory"][0]["sections"]

        # The barrier (main thread, owns `events`) emitted a gap event.
        count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='sut_map.probe_gap';"
        ).fetchone()[0]
        assert count >= 1

        # Planning still proceeds on the degraded-but-valid map.
        plan_work_item(conn, paths, events, work_item_id=work_id)
        assert (
            paths.runtime_root / "plans" / work_id / "TEST-PLAN.json"
        ).exists()
    finally:
        conn.close()


def test_plan_emits_test_plan_json(tmp_path: Path) -> None:
    conn, paths, events, work_id = _setup_repo(tmp_path)
    try:
        analyze_work_item(conn, paths, events, work_item_id=work_id)
        plan_result = plan_work_item(conn, paths, events, work_item_id=work_id)
        json_path = paths.runtime_root / "plans" / work_id / "TEST-PLAN.json"
        assert json_path.exists()
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert payload["version"] == "1.0"
        assert payload["task_id"] == work_id
        # Each OpenAPI op should produce a PlanItem.
        candidates = payload["items"]
        assert len(candidates) >= 2
        assert all(c["decision"] == "needs_operator_decision" for c in candidates)
        assert any(c["target_path"] == "/orders" for c in candidates)
    finally:
        conn.close()


def test_operator_can_approve_plan_candidate(tmp_path: Path) -> None:
    conn, paths, events, work_id = _setup_repo(tmp_path)
    try:
        analyze_work_item(conn, paths, events, work_item_id=work_id)
        plan_work_item(conn, paths, events, work_item_id=work_id)
        candidates = read_plan_candidates(paths, work_item_id=work_id)["items"]
        target = next(c for c in candidates if c["target_path"] == "/orders" and c["target_method"] == "GET")

        result = update_plan_candidate_decision(
            paths,
            work_item_id=work_id,
            candidate_id=target["candidate_id"],
            decision="generate_now",
            expected_assertion="GET /orders must return HTTP 200 and a JSON array",
            cleanup_strategy="read-only endpoint",
            reason="safe read-only smoke",
        )

        assert result["summary"]["generate_now"] == 1
        refreshed = read_plan_candidates(paths, work_item_id=work_id)["items"]
        approved = next(c for c in refreshed if c["candidate_id"] == target["candidate_id"])
        assert approved["decision"] == "generate_now"
    finally:
        conn.close()


def test_concurrent_approvals_both_persist(tmp_path: Path) -> None:
    """Issue #161 — two parallel approvals of different candidates on
    the same TEST-PLAN.json must both stick. Without the per-target
    file lock in :func:`update_plan_candidate_decision` this used to be
    a last-write-wins race: thread B read the pre-write payload and its
    ``os.replace`` clobbered thread A's decision.
    """
    import threading

    conn, paths, events, work_id = _setup_repo(tmp_path)
    try:
        analyze_work_item(conn, paths, events, work_item_id=work_id)
        plan_work_item(conn, paths, events, work_item_id=work_id)
        candidates = read_plan_candidates(paths, work_item_id=work_id)["items"]
        target_a = next(c for c in candidates if c["target_method"] == "GET")
        target_b = next(c for c in candidates if c["target_method"] == "POST")

        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def approve(cid: str, assertion: str):
            try:
                barrier.wait()
                update_plan_candidate_decision(
                    paths,
                    work_item_id=work_id,
                    candidate_id=cid,
                    decision="generate_now",
                    expected_assertion=assertion,
                    cleanup_strategy="read-only endpoint",
                    reason="concurrent approve test",
                )
            except BaseException as exc:  # noqa: BLE001 — collect for assert
                errors.append(exc)

        t1 = threading.Thread(
            target=approve,
            args=(target_a["candidate_id"], "GET /orders must return HTTP 200 and a JSON array"),
        )
        t2 = threading.Thread(
            target=approve,
            args=(target_b["candidate_id"], "POST /orders must return HTTP 201 with the created id"),
        )
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, errors
        refreshed = read_plan_candidates(paths, work_item_id=work_id)["items"]
        by_id = {c["candidate_id"]: c for c in refreshed}
        assert by_id[target_a["candidate_id"]]["decision"] == "generate_now"
        assert by_id[target_b["candidate_id"]]["decision"] == "generate_now"
    finally:
        conn.close()


def test_implement_tests_blocks_when_no_candidate_is_approved(tmp_path: Path) -> None:
    conn, paths, events, work_id = _setup_repo(tmp_path)
    try:
        analyze_work_item(conn, paths, events, work_item_id=work_id)
        plan_work_item(conn, paths, events, work_item_id=work_id)

        result = implement_tests_for_work_item(conn, paths, events, work_item_id=work_id)

        assert result["status"] == "blocked"
        assert result["executable_tests_generated"] is False
        assert result["generated_v2"]["reason"] == "no_generate_now_items"
    finally:
        conn.close()


def test_implement_tests_emits_v2_when_plan_has_generate_now(tmp_path: Path) -> None:
    conn, paths, events, work_id = _setup_repo(tmp_path)
    try:
        analyze_work_item(conn, paths, events, work_item_id=work_id)
        plan_work_item(conn, paths, events, work_item_id=work_id)
        # Promote one plan item to generate_now manually + flesh out fields.
        json_path = paths.runtime_root / "plans" / work_id / "TEST-PLAN.json"
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        first = payload["items"][0]
        first["decision"] = "generate_now"
        first["expected_assertion"] = "POST /orders must return HTTP 201 and body.id present"
        first["cleanup_strategy"] = "DELETE /orders/{id}"
        # Issue #94 — mutating tests need JSON test data, not free text.
        first["required_test_data"] = '{"sku": "DEMO-1", "quantity": 1}'
        # Issue #105 — functional/lifecycle metadata required for generate_now.
        first["functional_area"] = "functional-orders"
        first["lifecycle_tags"] = ["regression"]
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        result = implement_tests_for_work_item(conn, paths, events, work_item_id=work_id)
        v2 = result.get("generated_v2") or {}
        assert "manifest" in v2, f"expected v2 manifest, got: {v2}"
        assert v2["manifest"]["files"], "expected at least one generated spec"
        gen_path = paths.repo_root / v2["output_dir"] / "files" / v2["manifest"]["files"][0]["relative_path"]
        assert gen_path.is_file()
        content = gen_path.read_text(encoding="utf-8")
        assert "expect(response.status()).toBe(201)" in content

        # Issue #72 — the returned `patch_path` must contain the
        # executable test files, not a Markdown skeleton, so that the
        # standard review-gate --apply-patch flow creates runnable tests.
        assert result["executable_tests_generated"] is True
        first_target = v2["manifest"]["files"][0]["relative_path"]
        assert result["target_path"] == first_target
        assert first_target.startswith("tests/")
        assert first_target.endswith(".spec.ts")
        assert first_target in result["executable_targets"]
        patch_text = (paths.repo_root / result["patch_path"]).read_text(encoding="utf-8")
        assert f"diff --git a/{first_target}" in patch_text
        # The Markdown skeleton path must NOT be in the apply patch.
        assert f"tests/generated/{work_id}.spec.md" not in patch_text
    finally:
        conn.close()


def test_implement_tests_executable_patch_applies_runnable_tests(tmp_path: Path) -> None:
    """Issue #72 acceptance — after promoting a candidate to `generate_now`,
    `git apply` on the returned `patch_path` must create the executable
    Playwright spec inside the real test tree."""
    import subprocess

    conn, paths, events, work_id = _setup_repo(tmp_path)
    try:
        analyze_work_item(conn, paths, events, work_item_id=work_id)
        plan_work_item(conn, paths, events, work_item_id=work_id)
        json_path = paths.runtime_root / "plans" / work_id / "TEST-PLAN.json"
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        first = payload["items"][0]
        first["decision"] = "generate_now"
        first["expected_assertion"] = "POST /orders must return HTTP 201 and body.id present"
        first["cleanup_strategy"] = "DELETE /orders/{id}"
        # Issue #94 — mutating tests need JSON test data, not free text.
        first["required_test_data"] = '{"sku": "DEMO-1", "quantity": 1}'
        # Issue #105 — functional/lifecycle metadata required for generate_now.
        first["functional_area"] = "functional-orders"
        first["lifecycle_tags"] = ["regression"]
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        result = implement_tests_for_work_item(conn, paths, events, work_item_id=work_id)
        patch_path = paths.repo_root / result["patch_path"]
        target_rel = result["target_path"]

        subprocess.run(["git", "-C", str(paths.repo_root), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(paths.repo_root), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(paths.repo_root), "config", "user.name", "Test"],
            check=True,
        )
        # `git apply --check` must accept the patch …
        subprocess.run(
            ["git", "-C", str(paths.repo_root), "apply", "--check", str(patch_path)],
            check=True,
        )
        # … and applying it must create the executable spec on disk.
        subprocess.run(
            ["git", "-C", str(paths.repo_root), "apply", str(patch_path)],
            check=True,
        )
        applied = (paths.repo_root / target_rel).read_text(encoding="utf-8")
        assert "expect(response.status()).toBe(201)" in applied
    finally:
        conn.close()
