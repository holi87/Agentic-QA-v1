"""Executable Playwright UI test generation behavior."""
from __future__ import annotations

import pytest

from agentic_os.errors import UsageError
from agentic_os.generators.ui import generate_ui_test, generate_ui_tests
from agentic_os.plan_v2 import PlanItem


def _order_smoke_item(**overrides) -> PlanItem:
    base = dict(
        candidate_id="UI-001",
        title="Order create smoke",
        test_type="ui",
        priority="P1",
        decision="generate_now",
        expected_assertion='After submit, URL must contain /orders/ and text "Order created" must be visible',
        source_refs=["docs/requirements.md#L80", "docs/ui-flows.md#order-create"],
        target_page="/orders/new",
        required_test_data="quantity=1",
        cleanup_strategy="reset via API teardown step",
        generator_target="playwright-ts",
        notes=[],
    )
    base.update(overrides)
    return PlanItem(**base)


def test_ui_generator_path_and_runner() -> None:
    gen = generate_ui_test(_order_smoke_item())
    assert gen.relative_path.endswith(".spec.ts")
    assert "tests/ui/" in gen.relative_path
    assert gen.runner == "playwright-ts"


def test_ui_generator_carries_sources_and_assertion() -> None:
    gen = generate_ui_test(_order_smoke_item())
    assert "docs/requirements.md#L80" in gen.content
    assert "docs/ui-flows.md#order-create" in gen.content
    assert "Order created" in gen.content


def test_ui_generator_emits_screenshot_and_trace_directive() -> None:
    gen = generate_ui_test(_order_smoke_item())
    assert "screenshot: 'only-on-failure'" in gen.content
    assert "trace: 'retain-on-failure'" in gen.content


def test_ui_generator_uses_semantic_locators() -> None:
    gen = generate_ui_test(_order_smoke_item())
    # The text-based hint should produce a getByText assertion.
    assert "getByText(" in gen.content
    # And NO brittle selectors.
    for bad in ("page.locator('div >", "page.locator('.css-", "xpath="):
        assert bad not in gen.content


def test_ui_generator_emits_url_assertion_from_plan_text() -> None:
    gen = generate_ui_test(_order_smoke_item())
    assert "toHaveURL" in gen.content
    assert "/orders/" in gen.content


def test_ui_generator_rejects_missing_target_page() -> None:
    with pytest.raises(UsageError):
        generate_ui_test(_order_smoke_item(target_page=None))


def test_ui_generator_rejects_brittle_selector_in_notes() -> None:
    with pytest.raises(UsageError):
        generate_ui_test(_order_smoke_item(notes=["selector: div > .css-12abc"]))


def test_ui_generator_credentials_env_storage_state() -> None:
    gen = generate_ui_test(_order_smoke_item(), credentials_env="SESSION")
    assert 'process.env["SESSION_STATE_PATH"]' in gen.content
    assert "storageState" in gen.content


def test_ui_generator_batch_skips_api_items() -> None:
    items = [
        _order_smoke_item(candidate_id="UI-OK"),
        _order_smoke_item(
            candidate_id="API-1",
            test_type="api",
            target_method="GET",
            target_path="/orders",
            target_page=None,
        ),
    ]
    out = generate_ui_tests(items)
    assert [g.candidate_id for g in out] == ["UI-OK"]


def test_ui_generator_uses_env_base_url() -> None:
    gen = generate_ui_test(_order_smoke_item())
    assert 'process.env["UI_BASE_URL"]' in gen.content
    assert 'const targetPage = "/orders/new";' in gen.content
    assert "page.goto(new URL(targetPage, UI_BASE_URL).toString())" in gen.content


def test_ui_generator_deterministic_filename() -> None:
    a = generate_ui_test(_order_smoke_item())
    b = generate_ui_test(_order_smoke_item())
    assert a.relative_path == b.relative_path
    assert a.content == b.content


def test_ui_generator_role_and_name_hint_produces_getByRole() -> None:
    gen = generate_ui_test(
        _order_smoke_item(
            candidate_id="UI-ROLE",
            expected_assertion='Submit button with role "button" name "Create order" must be enabled',
            notes=[],
        )
    )
    assert 'getByRole("button"' in gen.content
    assert 'name: "Create order"' in gen.content
