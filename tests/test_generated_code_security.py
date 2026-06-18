"""Issue #365 — security rules for generated test code.

The issue's body predates ADR-0002 (RestAssured `blacklistHeader`, OWASP
dependency-check, Maven). Reframed to Playwright + TypeScript: the rules live in
`docs/standards/playwright-ts-standards.md` §8 (+ `_pl` twin), and the
generators already comply — base URLs and credentials are env-injected, never
hard-coded, and never logged. These tests lock that compliance (acceptance:
"generated tests read config from env; logs contain no secrets") and assert the
standard documents it.
"""
from __future__ import annotations

from pathlib import Path

from agentic_os.generators.api import generate_api_test
from agentic_os.plan_v2 import PlanItem

_DOCS = Path(__file__).resolve().parent.parent / "docs" / "standards"


def _api_item() -> PlanItem:
    return PlanItem(
        candidate_id="API-OAS-CREATEORDER",
        title="Create order",
        test_type="api",
        priority="P2",
        decision="generate_now",
        expected_assertion="POST /orders must return HTTP 201 and body.id present",
        source_refs=["docs/openapi.yaml#/orders/post"],
        target_method="POST",
        target_path="/orders",
        required_test_data='{"sku":"DEMO-1","quantity":1}',
        cleanup_strategy="DELETE /orders/{id}",
    )


def test_generated_api_spec_injects_base_url_and_creds_from_env() -> None:
    spec = generate_api_test(_api_item(), credentials_env="SUT_API_TOKEN")
    content = spec.content
    # Base URL is read from the environment, not hard-coded.
    assert "process.env[" in content
    assert "API_BASE_URL" in content
    # The Authorization header is built from an env var AT RUNTIME — the env var
    # NAME is in the source, never a secret value.
    assert 'process.env["SUT_API_TOKEN"]' in content
    assert "Authorization" in content


def test_generated_api_spec_does_not_log_or_hardcode_secrets() -> None:
    spec = generate_api_test(_api_item(), credentials_env="SUT_API_TOKEN")
    content = spec.content
    # No console logging that could leak a token / header / body.
    assert "console.log" not in content
    # A bearer token must be an env interpolation, never a literal value.
    assert "Bearer ${process.env" in content


def test_security_section_documented_in_both_standards_docs() -> None:
    en = (_DOCS / "playwright-ts-standards.md").read_text(encoding="utf-8")
    pl = (_DOCS / "playwright-ts-standards_pl.md").read_text(encoding="utf-8")
    # Section present in both (EN + the _pl twin).
    assert "## 8. Security" in en
    assert "## 8. Bezpieczeństwo" in pl
    # Each rule the acceptance names is prescribed in both docs.
    for doc in (en, pl):
        assert "process.env" in doc          # env-injection, no hard-coded secrets
        assert "npm ci" in doc               # pinned, reproducible deps
        assert "Authorization" in doc        # credentials never logged / captured
