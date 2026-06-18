"""Issue #370 — two-mode execution contract (in-container OS run vs standalone
on host), with SUT URLs injected via env.

The issue body proposes `SUT_WEB_URL`/`SUT_API_URL`, but the runtime and the
generators actually consume `UI_BASE_URL` / `API_BASE_URL` (issue #92, the
standalone scaffold #369). This contract documents REALITY: the *same* suite
runs in both modes by changing only environment values. These tests pin the doc
to the code — the env-var names the contract names must be the ones the
generated specs and the standalone runner actually read.
"""
from __future__ import annotations

from pathlib import Path

from agentic_os.generators.api import generate_api_test
from agentic_os.generators.ui import generate_ui_test
from agentic_os.plan_v2 import PlanItem
from agentic_os.standalone import SCAFFOLD_DIR

_DOCS = Path(__file__).resolve().parent.parent / "docs"
_CONTRACT = _DOCS / "two-mode-execution-contract.md"
_CONTRACT_PL = _DOCS / "two-mode-execution-contract_pl.md"


def _api_item() -> PlanItem:
    return PlanItem(
        candidate_id="API-OAS-X", title="x", test_type="api", priority="P2",
        decision="generate_now", expected_assertion="GET /x must return HTTP 200",
        source_refs=["docs/openapi.yaml#/x/get"], target_method="GET",
        target_path="/x", cleanup_strategy="read-only endpoint",
    )


def _ui_item() -> PlanItem:
    return PlanItem(
        candidate_id="UI-X", title="x", test_type="ui", priority="P2",
        decision="generate_now",
        expected_assertion='URL must contain /x and text "ok" is shown',
        source_refs=["docs/requirements.md#x"], target_page="/x",
    )


def test_contract_doc_exists_in_both_languages() -> None:
    assert _CONTRACT.exists(), "missing two-mode execution contract doc"
    assert _CONTRACT_PL.exists(), "missing _pl twin"


def test_contract_documents_both_run_modes_and_networking() -> None:
    doc = _CONTRACT.read_text(encoding="utf-8")
    # Both modes named, framed as additive (not either/or).
    assert "in-container" in doc.lower() or "inside the" in doc.lower()
    assert "standalone" in doc.lower()
    # The networking caveat is referenced, not duplicated.
    assert "host.docker.internal" in doc
    assert "docker-networking-contract" in doc


def test_contract_env_vars_match_what_the_code_consumes() -> None:
    # The real contract surface — assert the doc names exactly these, and that
    # the generated specs + standalone runner actually read them.
    doc = _CONTRACT.read_text(encoding="utf-8")
    assert "API_BASE_URL" in doc
    assert "UI_BASE_URL" in doc

    api_spec = generate_api_test(_api_item()).content
    ui_spec = generate_ui_test(_ui_item()).content
    run_sh = (SCAFFOLD_DIR / "run-tests.sh").read_text(encoding="utf-8")
    assert "API_BASE_URL" in api_spec
    assert "UI_BASE_URL" in ui_spec
    assert "API_BASE_URL" in run_sh and "UI_BASE_URL" in run_sh


def test_pl_twin_names_the_same_env_contract() -> None:
    pl = _CONTRACT_PL.read_text(encoding="utf-8")
    assert "API_BASE_URL" in pl
    assert "UI_BASE_URL" in pl
    assert "host.docker.internal" in pl
