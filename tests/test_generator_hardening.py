"""Regressions for the Wave 2 generator hardening (issues #88, #91, #94,
#95, #99, #105, #106, #107).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_os.errors import UsageError
from agentic_os.generators.api import generate_api_test
from agentic_os.generators.ui import generate_ui_test
from agentic_os.plan_v2 import PlanItem, validate_plan


def _api_item(**overrides) -> PlanItem:
    base = dict(
        candidate_id="API-CASE-1",
        title="Reject negative quantity",
        test_type="api",
        priority="P1",
        decision="generate_now",
        expected_assertion="POST /orders with quantity=-1 must return HTTP 400 and body.error.code = \"invalid_quantity\"",
        source_refs=["docs/openapi.yaml#/paths/~1orders/post"],
        target_method="POST",
        target_path="/orders",
        required_test_data='{"quantity": -1}',
        cleanup_strategy="rejection path — no resource to clean",
        generator_target="playwright-ts",
        functional_area="functional-orders",
        lifecycle_tags=["regression"],
    )
    base.update(overrides)
    return PlanItem(**base)


def _ui_item(**overrides) -> PlanItem:
    base = dict(
        candidate_id="UI-CASE-1",
        title="Checkout shows validation error",
        test_type="ui",
        priority="P1",
        decision="generate_now",
        expected_assertion="text \"Order created\" must be visible",
        source_refs=["docs/specs/orders.md#checkout"],
        target_page="/checkout",
        generator_target="playwright-ts",
        functional_area="functional-orders",
        lifecycle_tags=["smoke"],
    )
    base.update(overrides)
    return PlanItem(**base)


# Issue #94 — free-text test data must fail mutating generation.
def test_api_generator_rejects_free_text_test_data() -> None:
    with pytest.raises(UsageError, match="must be JSON"):
        generate_api_test(_api_item(required_test_data="valid user payload"))


def test_api_generator_accepts_json_test_data() -> None:
    spec = generate_api_test(_api_item(required_test_data='{"quantity": -1}'))
    assert 'JSON.parse("{\\"quantity\\": -1}")' in spec.content


# Issue #91 — mutating tests need executable cleanup.
def test_api_generator_emits_executable_cleanup_for_delete_directive() -> None:
    spec = generate_api_test(
        _api_item(
            required_test_data='{"sku": "A"}',
            cleanup_strategy="DELETE /orders/{id}",
        )
    )
    assert 'await ctx.delete("/orders/{id}")' in spec.content
    assert "try {" in spec.content


def test_api_generator_rejects_freeform_cleanup() -> None:
    with pytest.raises(UsageError, match="cleanup_strategy for"):
        generate_api_test(
            _api_item(
                required_test_data='{"sku": "A"}',
                cleanup_strategy="please clean later",
            )
        )


# Issue #95 — body assertions must compile to executable checks.
def test_api_generator_emits_body_field_assertion() -> None:
    spec = generate_api_test(
        _api_item(
            expected_assertion='POST /orders must return HTTP 201 and body.id present',
        )
    )
    assert "expect(body.id" in spec.content


def test_api_generator_rejects_unparseable_body_intent() -> None:
    with pytest.raises(UsageError, match="body/header"):
        generate_api_test(
            _api_item(
                expected_assertion="POST /orders must return HTTP 201 and an opaque body",
            )
        )


# Issue #88 — UI generator must load storageState before the test.
def test_ui_generator_loads_storage_state_before_test() -> None:
    spec = generate_ui_test(_ui_item(), credentials_env="USER_TOKEN")
    # The fix uses module-level test.use({ storageState }) when an
    # auth state path is provided, rather than calling
    # context.storageState({ path }) which saves state.
    assert "test.use({ storageState: _AUTH_STATE_PATH })" in spec.content
    # The old (incorrect) save-state call must not appear.
    assert "context.storageState({ path:" not in spec.content


# Issue #105 — functional/lifecycle metadata required.
def test_plan_validator_requires_functional_area_and_lifecycle_tags() -> None:
    findings = validate_plan([_api_item(functional_area=None)])
    assert any("functional_area" in f.message for f in findings)
    findings = validate_plan([_api_item(lifecycle_tags=[])])
    assert any("lifecycle_tag" in f.message for f in findings)


# Issue #107 — pytest-httpx target is blocked.
def test_plan_validator_blocks_pytest_httpx_target() -> None:
    findings = validate_plan([_api_item(generator_target="pytest-httpx")])
    assert any("pytest-httpx is not implemented" in f.message for f in findings)


# Issue #106 — security/accessibility cannot generate_now.
def test_plan_validator_blocks_security_generate_now() -> None:
    findings = validate_plan(
        [_api_item(test_type="security")]
    )
    assert any("test_type=security" in f.message for f in findings)


def test_plan_validator_blocks_accessibility_generate_now() -> None:
    findings = validate_plan(
        [_api_item(test_type="accessibility")]
    )
    assert any("test_type=accessibility" in f.message for f in findings)


# Issue #92 — generated test env vars from config.
def test_run_tests_injects_api_and_ui_base_url_from_config(tmp_path: Path, monkeypatch) -> None:
    """`run_tests` must inject API_BASE_URL / UI_BASE_URL derived from
    `sut.api.url` / `sut.web.url` when those env vars aren't already
    set by the operator."""
    import textwrap

    from agentic_os.events import EventLog
    from agentic_os.orchestrator import Orchestrator
    from agentic_os.paths import RuntimePaths
    from agentic_os.storage import init_db

    repo = tmp_path / "repo"
    runtime_root = repo / ".agentic-os"
    paths = RuntimePaths(repo_root=repo, runtime_root=runtime_root)
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()

    # Minimum valid config with both URLs set.
    cfg = repo / ".qualitycat" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        textwrap.dedent(
            """\
            runtime:
              root: .agentic-os
              timezone: Europe/Warsaw
              max_parallel_tasks: 1
              heartbeat_seconds: 20
              lease_ttl_seconds: 60
              stale_lease_seconds: 90
              shutdown_grace_seconds: 1
              timeouts:
                default_seconds: 30
                docker_seconds: 30
                test_seconds: 30
                model_seconds: 30
                report_seconds: 30
            sut:
              root: .
              compose_file: docker-compose.yml
              compose_project_name: agentic-os-sut
              autostart: false
              api:
                enabled: true
                url: http://api.example/v1
              web:
                enabled: true
                url: http://web.example
              healthcheck:
                command: ["true"]
                timeout_seconds: 1
                retries: 0
              test_runner: ./run-tests.sh
              install_shim_allowed: false
            models:
              planner: {provider: claude, command: ["claude"], role: opus}
              implementer: {provider: claude, command: ["claude"], role: sonnet}
              reviewer: {provider: codex, command: ["codex"], role: codex}
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
            """
        ),
        encoding="utf-8",
    )

    # Runner that echoes both env vars to a log so the test can verify
    # they reached the subprocess.
    runner = repo / "run-tests.sh"
    runner.write_text(
        "#!/usr/bin/env bash\n"
        'echo "API_BASE_URL=${API_BASE_URL:-unset}" > /tmp/.agentic-env-probe.log\n'
        'echo "UI_BASE_URL=${UI_BASE_URL:-unset}" >> /tmp/.agentic-env-probe.log\n'
        "exit 0\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    # Minimal report scripts so finalize_reports does not fail outright.
    for name in ("copy-reports.sh", "extract-last-run.sh", "build-summary.sh"):
        p = repo / "scripts" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "#!/usr/bin/env bash\nmkdir -p reports\necho '{\"total\": 1, \"passed\": 1, "
            "\"failed\": 0, \"skipped\": 0, \"failures\": []}' > reports/last-run.json\n"
            "printf '# stub\\n' > reports/summary.md\nexit 0\n",
            encoding="utf-8",
        )
        p.chmod(0o755)

    monkeypatch.delenv("API_BASE_URL", raising=False)
    monkeypatch.delenv("UI_BASE_URL", raising=False)

    from agentic_os.workflows import run_tests

    run_tests(orch, paths, events)
    probe = Path("/tmp/.agentic-env-probe.log").read_text(encoding="utf-8")
    assert "API_BASE_URL=http://api.example/v1" in probe
    assert "UI_BASE_URL=http://web.example" in probe
    conn.close()


# Issue #99 — Playwright JSON report merges into last-run.json.
def test_extract_last_run_merges_playwright_report(tmp_path: Path) -> None:
    from agentic_os.events import EventLog
    from agentic_os.paths import RuntimePaths
    from agentic_os.qualitycat import _merge_playwright_report

    repo = tmp_path / "repo"
    repo.mkdir()
    pw = repo / "reports" / "playwright" / "report.json"
    pw.parent.mkdir(parents=True, exist_ok=True)
    pw.write_text(
        json.dumps(
            {
                "stats": {
                    "expected": 1,
                    "unexpected": 1,
                    "flaky": 0,
                    "skipped": 0,
                },
                "suites": [
                    {
                        "specs": [
                            {
                                "title": "checkout flow",
                                "file": "tests/ui/checkout.spec.ts",
                                "tests": [
                                    {
                                        "title": "validates card",
                                        "results": [
                                            {
                                                "status": "failed",
                                                "error": {
                                                    "message": "expected 422 got 200",
                                                    "stack": "stack-head",
                                                },
                                                "attachments": [
                                                    {
                                                        "name": "trace",
                                                        "path": "reports/playwright/test-results/trace.zip",
                                                    },
                                                    {
                                                        "name": "screenshot",
                                                        "path": "reports/playwright/test-results/shot.png",
                                                    },
                                                ],
                                            }
                                        ],
                                    }
                                ],
                            }
                        ]
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    base = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "failures": []}
    merged = _merge_playwright_report(base, pw)
    assert merged["total"] == 2
    assert merged["passed"] == 1
    assert merged["failed"] == 1
    assert merged["failures"][0]["scenario"] == "checkout flow"
    assert merged["failures"][0]["trace"].endswith("trace.zip")
    assert merged["failures"][0]["screenshot"].endswith("shot.png")
