"""Executable Playwright API test generation and patch artifact writing."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agentic_os.errors import UsageError
from agentic_os.generators.api import (
    generate_api_test,
    generate_api_tests,
    write_patch_artifact,
)
from agentic_os.plan_v2 import PlanItem


def _negative_quantity_item(**overrides) -> PlanItem:
    base = dict(
        candidate_id="C-001",
        title="Reject negative quantity",
        test_type="api",
        priority="P1",
        decision="generate_now",
        expected_assertion="POST /orders with quantity=-1 must return HTTP 400 and error.code=invalid_quantity",
        source_refs=[
            "docs/openapi.yaml#/paths/~1orders/post",
            "docs/requirements.md#L42",
        ],
        target_method="POST",
        target_path="/orders",
        required_test_data='{"quantity":-1}',
        cleanup_strategy="rejection path — no resource to clean",
        negative_or_boundary=True,
        generator_target="playwright-ts",
    )
    base.update(overrides)
    return PlanItem(**base)


def test_generator_emits_executable_spec_path() -> None:
    gen = generate_api_test(_negative_quantity_item())
    assert gen.relative_path.endswith(".spec.ts")
    assert "tests/api/" in gen.relative_path
    assert gen.candidate_id == "C-001"
    assert gen.runner == "playwright-ts"


def test_generator_includes_source_refs_as_comment() -> None:
    gen = generate_api_test(_negative_quantity_item())
    for ref in (
        "docs/openapi.yaml#/paths/~1orders/post",
        "docs/requirements.md#L42",
    ):
        assert ref in gen.content


def test_generator_carries_exact_assertion_text() -> None:
    gen = generate_api_test(_negative_quantity_item())
    # The plan's exact text appears in the file (audit trail + console).
    assert "HTTP 400" in gen.content
    assert "error.code=invalid_quantity" in gen.content


def test_generator_emits_http_status_assertion() -> None:
    gen = generate_api_test(_negative_quantity_item())
    assert "expect(response.status()).toBe(400);" in gen.content


def test_generator_emits_body_assertion_for_error_code() -> None:
    gen = generate_api_test(_negative_quantity_item())
    assert "body.error.code" in gen.content or "error: { code: 'invalid_quantity' }" in gen.content


def test_generator_does_not_emit_trivial_assertion() -> None:
    gen = generate_api_test(_negative_quantity_item())
    bad_patterns = [
        "expect(response.ok())",
        "response.ok)",
        "assert(true)",
        "expect(true)",
    ]
    for bad in bad_patterns:
        assert bad not in gen.content, f"generator emitted trivial assertion: {bad}"


def test_generator_uses_env_var_for_base_url() -> None:
    gen = generate_api_test(_negative_quantity_item())
    assert 'process.env["API_BASE_URL"]' in gen.content
    # No literal hostnames hardcoded in generated code.
    assert "http://" not in gen.content or 'process.env["API_BASE_URL"]' in gen.content


def test_generator_emits_cleanup_comment_for_mutating_method() -> None:
    """Issue #91 — `rejection path` markers stay no-teardown comments."""
    gen = generate_api_test(_negative_quantity_item())
    assert "no teardown call emitted by design" in gen.content
    assert "rejection path" in gen.content


def test_generator_uses_credentials_env_ref_when_provided() -> None:
    gen = generate_api_test(
        _negative_quantity_item(),
        credentials_env="TEST_USER_TOKEN",
    )
    assert 'process.env["TEST_USER_TOKEN"]' in gen.content
    assert "Authorization" in gen.content
    # The literal token value never appears.
    assert "TEST_USER_TOKEN_value" not in gen.content


def test_generator_rejects_missing_source_ref() -> None:
    with pytest.raises(UsageError):
        generate_api_test(_negative_quantity_item(source_refs=[]))


def test_generator_rejects_non_generate_now() -> None:
    with pytest.raises(UsageError):
        generate_api_test(_negative_quantity_item(decision="needs_operator_decision"))


def test_generator_skips_ui_and_non_generate_now_in_batch() -> None:
    items = [
        _negative_quantity_item(candidate_id="C-OK", decision="generate_now"),
        _negative_quantity_item(candidate_id="C-UI", test_type="ui", target_method=None, target_path=None, target_page="/orders"),
        _negative_quantity_item(candidate_id="C-SKIP", decision="needs_operator_decision"),
    ]
    out = generate_api_tests(items)
    ids = [g.candidate_id for g in out]
    assert ids == ["C-OK"]


def test_generator_emits_deterministic_filename() -> None:
    a = generate_api_test(_negative_quantity_item())
    b = generate_api_test(_negative_quantity_item())
    assert a.relative_path == b.relative_path
    assert a.content == b.content


def test_write_patch_artifact_emits_manifest_and_files(tmp_path: Path) -> None:
    tests = generate_api_tests([_negative_quantity_item()])
    out_dir = tmp_path / "patches" / "TASK-001" / "run-1"
    manifest = write_patch_artifact(tests, output_dir=out_dir)
    assert (out_dir / "manifest.json").is_file()
    persisted = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert persisted["files"][0]["candidate_id"] == "C-001"
    target = out_dir / "files" / tests[0].relative_path
    assert target.is_file()
    assert "expect(response.status()).toBe(400)" in target.read_text(encoding="utf-8")


def test_generated_test_has_unique_test_title() -> None:
    gen = generate_api_test(_negative_quantity_item())
    # title contains candidate_id so multiple specs do not collide in reports
    assert 'test("C-001 \\u2014' in gen.content or 'test("C-001 —' in gen.content


def test_generator_handles_get_without_body() -> None:
    gen = generate_api_test(
        _negative_quantity_item(
            candidate_id="C-GET",
            target_method="GET",
            target_path="/orders",
            required_test_data="",
            cleanup_strategy="read-only",
            expected_assertion="GET /orders must return HTTP 200 and a JSON array",
        )
    )
    assert 'ctx.get("/orders")' in gen.content
    assert "expect(response.status()).toBe(200)" in gen.content
