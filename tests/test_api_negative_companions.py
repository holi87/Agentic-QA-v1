"""API generator negative-coverage companion blocks."""
from __future__ import annotations

import pytest

from agentic_os.generators.api import generate_api_test
from agentic_os.plan_v2 import PlanItem


def _get_item(**overrides) -> PlanItem:
    base = dict(
        candidate_id="API-231",
        title="List orders",
        test_type="api",
        priority="P1",
        decision="generate_now",
        expected_assertion="HTTP 200 and body.orders present",
        source_refs=["docs/api.md#L42"],
        target_method="GET",
        target_path="/orders",
        cleanup_strategy=None,
        notes=[],
    )
    base.update(overrides)
    return PlanItem(**base)


def _post_item(**overrides) -> PlanItem:
    base = dict(
        candidate_id="API-POST",
        title="Create order",
        test_type="api",
        priority="P1",
        decision="generate_now",
        expected_assertion="HTTP 201 and body.id present",
        source_refs=["docs/api.md#L80"],
        target_method="POST",
        target_path="/orders",
        required_test_data='{"quantity": 1}',
        cleanup_strategy="DELETE /orders/{id}",
        notes=[],
    )
    base.update(overrides)
    return PlanItem(**base)


def _id_item(**overrides) -> PlanItem:
    base = dict(
        candidate_id="API-ID",
        title="Get order by id",
        test_type="api",
        priority="P1",
        decision="generate_now",
        expected_assertion="HTTP 200 and body.id present",
        source_refs=["docs/api.md#L120"],
        target_method="GET",
        target_path="/orders/{id}",
        notes=[],
    )
    base.update(overrides)
    return PlanItem(**base)


def test_api_companion_floor_off_keeps_baseline() -> None:
    spec = generate_api_test(_get_item()).content
    for marker in (
        "agentic-os:companion:neg-auth",
        "agentic-os:companion:bola",
        "agentic-os:companion:boundary",
        "agentic-os:companion:injection",
        "agentic-os:companion:schema",
    ):
        assert marker not in spec
    # Happy-path preserved.
    assert "expect(response.status()).toBe(200)" in spec


def test_get_with_credentials_emits_neg_auth_and_schema_only() -> None:
    spec = generate_api_test(
        _get_item(), credentials_env="SESSION", coverage_floor=True
    ).content
    assert "agentic-os:companion:neg-auth" in spec
    assert "agentic-os:companion:schema" in spec
    # GET without {id} → no BOLA, no boundary, no injection.
    assert "agentic-os:companion:bola" not in spec
    assert "agentic-os:companion:boundary" not in spec
    assert "agentic-os:companion:injection" not in spec


def test_id_path_emits_bola_when_credentials_set() -> None:
    spec = generate_api_test(
        _id_item(), credentials_env="SESSION", coverage_floor=True
    ).content
    assert "agentic-os:companion:bola" in spec
    assert 'process.env["SESSION_OTHER_ID"]' in spec
    assert "test.skip(!_otherId" in spec


def test_id_path_skips_bola_without_credentials() -> None:
    spec = generate_api_test(_id_item(), coverage_floor=True).content
    assert "agentic-os:companion:bola" not in spec
    assert "agentic-os:companion:neg-auth" not in spec


def test_post_emits_full_companion_set() -> None:
    spec = generate_api_test(
        _post_item(), credentials_env="SESSION", coverage_floor=True
    ).content
    for marker in (
        "agentic-os:companion:neg-auth",
        "agentic-os:companion:boundary",
        "agentic-os:companion:injection",
        "agentic-os:companion:schema",
    ):
        assert marker in spec, f"missing {marker}"
    # Boundary block emits both empty + oversize tests.
    assert "boundary-empty" in spec
    assert "boundary-oversize" in spec


def test_post_companions_use_separate_test_blocks() -> None:
    """Companion failures must not mask the happy-path verdict."""
    spec = generate_api_test(
        _post_item(), credentials_env="SESSION", coverage_floor=True
    ).content
    # Happy path + neg-auth + boundary-empty + boundary-oversize +
    # injection + schema = 6 separate test() declarations.
    assert spec.count("test(") >= 6


def test_injection_canary_probes_sqli_and_xss() -> None:
    spec = generate_api_test(
        _post_item(), credentials_env="SESSION", coverage_floor=True
    ).content
    assert "' OR 1=1 --" in spec
    assert "<script>x</script>" in spec
    assert ".not.toBe(500)" in spec


def test_no_negative_companions_opt_out() -> None:
    spec = generate_api_test(
        _post_item(notes=["no-negative-companions"]),
        credentials_env="SESSION",
        coverage_floor=True,
    ).content
    assert "agentic-os:companion:neg-auth" not in spec
    assert "agentic-os:companion:boundary" not in spec
    assert "agentic-os:companion:injection" not in spec


def test_schema_companion_uses_dynamic_ajv_import() -> None:
    """ajv is optional in the operator workspace."""
    spec = generate_api_test(_post_item(), coverage_floor=True).content
    assert "await import('ajv')" in spec
    assert "} catch (e) {" in spec
