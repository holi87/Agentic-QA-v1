"""Autonomous triage classifier rule table and batch flag behavior."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentic_os.triage_classifier import (
    KnownBug,
    classify_failure,
    load_known_bugs,
    triage_batch_enabled,
)


# --- Infra short-circuit ---------------------------------------------------

@pytest.mark.parametrize(
    "msg",
    [
        "fetch failed: ECONNREFUSED 127.0.0.1:5432",
        "connection refused on port 8080",
        "port 8080 is busy",
        "Error: getaddrinfo ENOTFOUND api.example.com",
        "docker daemon not running",
        "DNS resolution failed for sut.local",
    ],
)
def test_infra_failures_skip_bug_creation(msg: str) -> None:
    decision = classify_failure({"error_message": msg})
    assert decision.action == "skip_infra"
    assert decision.rule.startswith("infra:")


# --- Fingerprint match -----------------------------------------------------

def test_fingerprint_match_returns_append_known_bug() -> None:
    known = [
        KnownBug(
            bug_id="BUG-014-payment-fails",
            status=500,
            endpoint="POST /orders",
            assertion_text="payment provider returned 500",
        )
    ]
    failure = {
        "status_code": 500,
        "endpoint": "POST /orders",
        "error_message": "Payment provider returned 500",
    }
    decision = classify_failure(failure, known_bugs=known)
    assert decision.action == "append_known_bug"
    assert decision.bug_id == "BUG-014-payment-fails"
    assert "@known-bug" in decision.tags_to_append
    assert "@bug-14" in decision.tags_to_append


def test_fingerprint_within_levenshtein_5() -> None:
    known = [
        KnownBug(
            bug_id="BUG-002-foo",
            status=400,
            endpoint="GET /items",
            assertion_text="expected 200 got 400",
        )
    ]
    # One typo in assertion (got/gott) — distance 1.
    failure = {
        "status_code": 400,
        "endpoint": "GET /items",
        "error_message": "expected 200 gott 400",
    }
    decision = classify_failure(failure, known_bugs=known)
    assert decision.action == "append_known_bug"


def test_fingerprint_beyond_distance_falls_through() -> None:
    known = [
        KnownBug(
            bug_id="BUG-003-unrelated",
            status=500,
            endpoint="POST /widgets",
            assertion_text="totally different failure",
        )
    ]
    failure = {
        "status_code": 200,
        "endpoint": "GET /orders",
        "error_message": "something else entirely",
    }
    decision = classify_failure(failure, known_bugs=known)
    assert decision.action != "append_known_bug"


# --- OWASP unambiguous → auto-create S1/P1 ---------------------------------

def test_owasp_api1_with_status_200_creates_s1_bug() -> None:
    failure = {
        "tags": ["@owasp-api1", "@critical"],
        "status_code": 200,
        "error_message": "Foreign user id returned 200",
    }
    decision = classify_failure(failure)
    assert decision.action == "auto_create_bug"
    assert decision.severity == "S1"
    assert decision.priority == "P1"
    assert decision.rule.startswith("owasp:api1")


def test_owasp_tag_without_status_match_queues_operator() -> None:
    failure = {
        "tags": ["@owasp-api1"],
        "status_code": 401,  # the expected behavior is 401/403 — no breach.
        "error_message": "auth working as designed",
    }
    decision = classify_failure(failure)
    assert decision.action != "auto_create_bug"


# --- Spec-mirroring deterministic ≤ S2 ------------------------------------

def test_deterministic_spec_failure_auto_creates_bug() -> None:
    failure = {
        "tags": ["@s2"],
        "feature_uri": "features/orders.feature",
        "error_message": "expected 200 got 500",
        "run_count": 3,
    }
    decision = classify_failure(failure)
    assert decision.action == "auto_create_bug"
    assert decision.severity == "S2"
    assert decision.priority == "P2"


def test_single_run_does_not_meet_deterministic_threshold() -> None:
    failure = {
        "tags": ["@s2"],
        "feature_uri": "features/orders.feature",
        "error_message": "expected 200 got 500",
        # run_count missing → not deterministic per the issue spec.
    }
    decision = classify_failure(failure)
    assert decision.action == "queue_operator"


def test_no_spec_citation_falls_through_to_operator() -> None:
    failure = {
        "tags": ["@s3"],
        "error_message": "ambiguous failure",
        "run_count": 3,
    }
    decision = classify_failure(failure)
    assert decision.action == "queue_operator"


# --- Default queue --------------------------------------------------------

def test_unclassifiable_failure_queues_operator() -> None:
    failure = {"error_message": "something strange happened"}
    decision = classify_failure(failure)
    assert decision.action == "queue_operator"
    assert decision.rule == "low-confidence"


# --- Bugs index loader ----------------------------------------------------

def test_load_known_bugs_parses_frontmatter(tmp_path: Path) -> None:
    bugs_dir = tmp_path / "bugs"
    bugs_dir.mkdir()
    (bugs_dir / "BUG-001-foo.md").write_text(
        "---\nseverity: S2\nscenario: payment fails on retry\n---\n\n"
        "Stack: POST /payments returned HTTP 500\n",
        encoding="utf-8",
    )
    bugs = load_known_bugs(bugs_dir)
    assert len(bugs) == 1
    assert bugs[0].bug_id == "BUG-001-foo"
    assert bugs[0].status == 500
    assert bugs[0].endpoint == "POST /payments"


def test_load_known_bugs_handles_missing_dir(tmp_path: Path) -> None:
    assert load_known_bugs(tmp_path / "missing") == []


# --- Flag reader -----------------------------------------------------------

def test_triage_batch_flag_defaults_off(tmp_path: Path) -> None:
    """Missing config or read error must default to False."""
    assert triage_batch_enabled(tmp_path) is False
