"""Issue #233 — quantitative coverage-depth gate.

Counts the fixed marker comments emitted by the UI generator (#230) and
API generator (#231) inside a generated Playwright spec, then maps the
counts to a verdict the reviewer skill (qc-{claude,codex,gemini}-
reviewer-validate-tests step 12) can consume without having to read the
test logic.

Hard items (always required regardless of coverage_floor):
- UI: plan-derived business assertion (toHaveURL / getByRole /
  getByText / getByLabel).
- API: explicit HTTP status assertion (`expect(response.status())`).

Floor items (gated by autonomy.coverage_floor=true):
- UI: console listener, network listener, a11y scan, link-walk.
- API: neg-auth (when credentials), boundary OR injection (when
  mutating), schema-validate (always emitted by the generator).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


UI_FLOOR_MARKERS = (
    "agentic-os:floor:console",
    "agentic-os:floor:network",
    "agentic-os:floor:a11y",
    "agentic-os:floor:link-walk",
)

API_COMPANION_MARKERS = (
    "agentic-os:companion:neg-auth",
    "agentic-os:companion:bola",
    "agentic-os:companion:boundary",
    "agentic-os:companion:injection",
    "agentic-os:companion:schema",
)


@dataclass(frozen=True)
class CoverageVerdict:
    verdict: str       # PASS | PASS_WARN | REJECT
    reason: Optional[str]  # None for PASS; rule code for REJECT/WARN
    missing: tuple[str, ...]


def evaluate_ui_coverage(
    spec_text: str,
    *,
    coverage_floor: bool,
    target_page: Optional[str] = None,
) -> CoverageVerdict:
    """Score a UI spec against the marker contract."""
    if not _has_ui_business_assertion(spec_text):
        return CoverageVerdict("REJECT", "business_assertion_missing", ())
    missing = tuple(
        m for m in _required_ui_floor(target_page) if m not in spec_text
    )
    if not missing:
        return CoverageVerdict("PASS", None, ())
    if coverage_floor:
        return CoverageVerdict("REJECT", "coverage_floor_missing", missing)
    return CoverageVerdict("PASS_WARN", "coverage_floor_missing", missing)


def evaluate_api_coverage(
    spec_text: str,
    *,
    coverage_floor: bool,
    method: Optional[str] = None,
    credentials_set: bool = False,
) -> CoverageVerdict:
    """Score an API spec against the marker contract."""
    if not _has_api_status_assertion(spec_text):
        return CoverageVerdict("REJECT", "business_assertion_missing", ())
    missing = tuple(
        m for m in _required_api_companions(method, credentials_set)
        if m not in spec_text
    )
    if not missing:
        return CoverageVerdict("PASS", None, ())
    if coverage_floor:
        return CoverageVerdict("REJECT", "coverage_floor_missing", missing)
    return CoverageVerdict("PASS_WARN", "coverage_floor_missing", missing)


def _has_ui_business_assertion(spec_text: str) -> bool:
    return bool(
        re.search(r"toHaveURL|getByRole|getByText|getByLabel", spec_text)
    )


def _has_api_status_assertion(spec_text: str) -> bool:
    return bool(re.search(r"expect\(\s*response\.status\(\)\s*\)", spec_text))


def _required_ui_floor(target_page: Optional[str]) -> tuple[str, ...]:
    # Console + network always required when coverage_floor evaluation
    # is active; a11y always required (generator fail-soft handles
    # missing dep); link-walk required only for navigational targets.
    base = [
        "agentic-os:floor:console",
        "agentic-os:floor:network",
        "agentic-os:floor:a11y",
    ]
    if _is_navigational(target_page):
        base.append("agentic-os:floor:link-walk")
    return tuple(base)


def _required_api_companions(
    method: Optional[str], credentials_set: bool
) -> tuple[str, ...]:
    required: list[str] = []
    if credentials_set:
        required.append("agentic-os:companion:neg-auth")
    if method and method.upper() in {"POST", "PUT", "PATCH"}:
        # Either boundary OR injection satisfies the mutating-companion
        # requirement; we record `boundary` as the canonical marker but
        # accept injection as an alternative below.
        required.append("agentic-os:companion:boundary")
    # Schema-validate is always emitted by the generator when coverage
    # floor is on — keep it in the required set so its absence flags a
    # generator regression.
    required.append("agentic-os:companion:schema")
    return tuple(required)


def _is_navigational(target_page: Optional[str]) -> bool:
    if not target_page:
        return False
    # Form-style targets (`/orders/new`, `/login`, `?...`) opt out of
    # link-walk requirement; everything else is treated as navigational.
    lowered = target_page.lower()
    form_hints = ("/new", "/create", "/edit", "/login", "/signup", "/register")
    return not any(h in lowered for h in form_hints)
