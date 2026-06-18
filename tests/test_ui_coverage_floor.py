"""UI generator coverage-floor companion blocks."""
from __future__ import annotations

import pytest

from agentic_os.generators.ui import generate_ui_test
from agentic_os.plan_v2 import PlanItem


def _item(**overrides) -> PlanItem:
    base = dict(
        candidate_id="UI-230",
        title="Order list smoke",
        test_type="ui",
        priority="P1",
        decision="generate_now",
        expected_assertion='URL must contain /orders and text "Orders" must be visible',
        source_refs=["docs/requirements.md#L100"],
        target_page="/orders",
        notes=[],
    )
    base.update(overrides)
    return PlanItem(**base)


def test_ui_coverage_floor_off_keeps_baseline() -> None:
    spec = generate_ui_test(_item()).content
    # No floor markers when the flag is off — current behavior preserved.
    assert "agentic-os:floor:console" not in spec
    assert "agentic-os:floor:network" not in spec
    assert "agentic-os:floor:a11y" not in spec
    assert "agentic-os:floor:link-walk" not in spec
    # Baseline still functions.
    assert "page.goto(" in spec
    assert "toHaveURL" in spec


def test_coverage_floor_on_emits_all_markers() -> None:
    spec = generate_ui_test(_item(), coverage_floor=True).content
    for marker in (
        "agentic-os:floor:console",
        "agentic-os:floor:network",
        "agentic-os:floor:a11y",
        "agentic-os:floor:link-walk",
    ):
        assert marker in spec, f"missing {marker}"


def test_coverage_floor_console_listener_wired() -> None:
    spec = generate_ui_test(_item(), coverage_floor=True).content
    assert "page.on('pageerror'" in spec
    assert "page.on('console'" in spec
    assert "_consoleErrors" in spec
    assert "expect.soft(_consoleErrors" in spec


def test_coverage_floor_network_listener_wired() -> None:
    spec = generate_ui_test(_item(), coverage_floor=True).content
    assert "page.on('requestfailed'" in spec
    assert "_requestFailures" in spec
    assert "expect.soft(_requestFailures" in spec


def test_coverage_floor_a11y_uses_dynamic_axe_import() -> None:
    """Axe is optional in the operator workspace — must fail soft on import."""
    spec = generate_ui_test(_item(), coverage_floor=True).content
    assert "await import('@axe-core/playwright')" in spec
    assert "} catch (e) {" in spec  # fail-soft when axe missing


def test_coverage_floor_link_walk_opt_out() -> None:
    spec_in = generate_ui_test(_item(), coverage_floor=True).content
    spec_out = generate_ui_test(
        _item(notes=["no-link-walk"]), coverage_floor=True
    ).content
    assert "agentic-os:floor:link-walk" in spec_in
    assert "agentic-os:floor:link-walk" not in spec_out
    # Other floors still emitted when only link-walk is opted out.
    assert "agentic-os:floor:console" in spec_out


def test_coverage_floor_soft_asserts_never_mask_plan_assertion() -> None:
    """Hard plan assertion still uses `expect(...)` (not soft)."""
    spec = generate_ui_test(_item(), coverage_floor=True).content
    # Plan-derived assertion uses hard expect.
    assert "await expect(page).toHaveURL(" in spec
    # Floor companions use expect.soft.
    assert "expect.soft(_consoleErrors" in spec
    assert "expect.soft(_requestFailures" in spec


def test_coverage_floor_does_not_break_credentials_env() -> None:
    spec = generate_ui_test(
        _item(), coverage_floor=True, credentials_env="SESSION"
    ).content
    assert 'process.env["SESSION_STATE_PATH"]' in spec
    assert "agentic-os:floor:console" in spec


def test_autonomy_config_block_accepts_coverage_floor() -> None:
    """Coverage_floor flag is recognized by the validator."""
    from agentic_os.config import _validate

    base = _minimal_config()
    base["autonomy"] = {"coverage_floor": True}
    assert _validate(base) == []


def test_autonomy_config_block_rejects_non_bool() -> None:
    from agentic_os.config import _validate

    base = _minimal_config()
    base["autonomy"] = {"coverage_floor": "yes"}
    errors = _validate(base)
    assert any("autonomy.coverage_floor" in e for e in errors)


def test_autonomy_config_block_rejects_unknown_key() -> None:
    from agentic_os.config import _validate

    base = _minimal_config()
    base["autonomy"] = {"bogus_flag": True}
    errors = _validate(base)
    assert any("autonomy" in e for e in errors)


def _minimal_config() -> dict:
    return {
        "runtime": {
            "root": "agentic-os-runtime",
            "timezone": "Europe/Warsaw",
            "max_parallel_tasks": 1,
            "heartbeat_seconds": 10,
            "lease_ttl_seconds": 600,
            "stale_lease_seconds": 1800,
            "shutdown_grace_seconds": 30,
            "timeouts": {
                "default_seconds": 600,
                "docker_seconds": 120,
                "test_seconds": 900,
                "model_seconds": 600,
                "report_seconds": 120,
            },
        },
        "sut": {
            "root": ".",
            "compose_file": "docker-compose.yml",
            "compose_project_name": "app",
            "autostart": False,
            "healthcheck": {
                "command": ["sh", "-c", "exit 0"],
                "timeout_seconds": 5,
                "retries": 1,
            },
            "test_runner": "scripts/run-tests.sh",
            "install_shim_allowed": False,
        },
        "models": {
            "planner": {
                "provider": "claude",
                "command": ["claude", "--model", "opus"],
                "role": "opus",
            },
            "implementer": {
                "provider": "claude",
                "command": ["claude", "--model", "sonnet"],
                "role": "sonnet",
            },
            "reviewer": {
                "provider": "codex",
                "command": ["codex"],
                "role": "codex",
            },
        },
        "dashboard": {
            "host": "127.0.0.1",
            "port": 8765,
            "enable_write_endpoints": False,
        },
        "paths": {
            "reports": "reports",
            "bugs": "bugs",
            "evidence": "evidence",
            "prompts": "prompts",
        },
        "reports": {
            "copy_reports_script": "scripts/copy-reports.sh",
            "extract_last_run_script": "scripts/extract-last-run.sh",
            "build_summary_script": "scripts/build-summary.sh",
            "require_reports_on_failure": True,
        },
        "gates": {
            "known_bugs_fail_exit": True,
            "assertion_changes_require_decision": True,
            "exact_spec_failure_opens_bug": True,
            "require_functional_area_tag": True,
            "require_lifecycle_tag": True,
            "infrastructure_exit_code": 2,
        },
    }
