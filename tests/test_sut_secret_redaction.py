"""Issue #385 — log redaction honors config-declared SUT secret refs.

Subprocess redaction picks env values to scrub by matching the variable NAME
against a keyword heuristic. The external-SUT config can DECLARE which env var
holds a secret (``sut.db.value`` / ``sut.credentials.value`` with
``ref_type: env``) — and that name may carry no keyword (e.g. ``DATABASE_URL``).
Those declared values must be scrubbed regardless of the name heuristic.
"""
from __future__ import annotations

import sys
from pathlib import Path

from agentic_os.security import redact_sensitive_text, redaction_values_from_env
from agentic_os.runtime.subprocess import run_command
from agentic_os.workflows.stages.tests import _declared_secret_env_names

_DSN = "postgres://user:s3cr3t@db.internal:5432/app"


# ---- security.py: declared names scrubbed regardless of keyword -----------

def test_declared_env_name_collected_even_without_keyword() -> None:
    env = {"DATABASE_URL": _DSN}
    # Name heuristic alone misses it (no keyword in "DATABASE_URL").
    assert redaction_values_from_env(env) == []
    # Declared as a secret ref → its value is collected.
    assert _DSN in redaction_values_from_env(env, extra_names=["DATABASE_URL"])


def test_keyword_named_values_still_collected() -> None:
    env = {"API_TOKEN": "abcdef", "PLAIN": "hello"}
    values = redaction_values_from_env(env, extra_names=["DATABASE_URL"])
    assert "abcdef" in values
    assert "hello" not in values


def test_redact_sensitive_text_scrubs_declared_value() -> None:
    redacted = redact_sensitive_text(f"connecting to {_DSN} now", extra_values=[_DSN])
    assert _DSN not in redacted
    assert "[REDACTED:secret_in_env]" in redacted


# ---- subprocess integration: declared secret never reaches the log -------

def test_run_command_redacts_declared_secret_env(tmp_path: Path) -> None:
    log_path = tmp_path / "run.log"
    result = run_command(
        [sys.executable, "-c", "import os;print(os.environ.get('DATABASE_URL',''))"],
        cwd=tmp_path,
        log_path=log_path,
        timeout_seconds=30,
        env={"DATABASE_URL": _DSN},
        include_provider_credentials=False,
        secret_env_names=["DATABASE_URL"],
    )
    assert result.exit_code == 0
    log_text = log_path.read_text(encoding="utf-8")
    assert _DSN not in log_text, "declared SUT secret leaked into the run log"
    assert "[REDACTED:secret_in_env]" in log_text


# ---- tests.py helper: collect declared env names from sut config ---------

def test_declared_secret_env_names_from_sut_cfg() -> None:
    sut_cfg = {
        "db": {"ref_type": "env", "value": "DATABASE_URL"},
        "credentials": {"ref_type": "env", "value": "TEST_USER_TOKEN"},
    }
    assert _declared_secret_env_names(sut_cfg) == ["DATABASE_URL", "TEST_USER_TOKEN"]


def test_declared_secret_env_names_ignores_non_env_refs() -> None:
    sut_cfg = {
        "db": {"ref_type": "file", "value": "secrets/db.txt"},
        "credentials": {"ref_type": "none"},
    }
    assert _declared_secret_env_names(sut_cfg) == []
