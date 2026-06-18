"""Model invocation wrappers, prompt redaction, recording, and reviewer parsing."""
from __future__ import annotations

import os
import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest

from agentic_os.errors import InfraError, UsageError
from agentic_os.events import EventLog
from agentic_os.models import (
    ModelInvocationResult,
    invoke_model,
    parse_reviewer_invocation,
    redact_prompt,
)
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db


def _runtime(tmp_path: Path):
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    return conn, paths, events


def _write_script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o755)


def test_redact_prompt_masks_token_literals() -> None:
    text = "Bearer abc123token456\napi_key=SUPERSECRETKEY"
    safe = redact_prompt(text)
    assert "abc123token456" not in safe
    assert "SUPERSECRETKEY" not in safe
    assert "<redacted>" in safe


def test_invoke_model_records_invocation_row(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        # Write a fake CLI that echoes a prompt.
        fake = tmp_path / "bin" / "fake-claude"
        _write_script(
            fake,
            "#!/usr/bin/env bash\nprintf 'verdict: APPROVE\\nreason: ok\\n\\nfindings:\\n- OK:1 - none\\nREADY\\n'\n",
        )
        os.environ["PATH"] = str(fake.parent) + os.pathsep + os.environ["PATH"]
        try:
            result = invoke_model(
                conn,
                paths,
                events,
                role="reviewer",
                config={
                    "reviewer": {
                        "provider": "codex",
                        "command": [str(fake)],
                        "role": "codex",
                    }
                },
                prompt="please review",
                timeout_seconds=5,
            )
        finally:
            os.environ["PATH"] = os.environ["PATH"].replace(str(fake.parent) + os.pathsep, "")
        assert result.exit_code == 0
        assert result.role == "reviewer"
        # Persisted row.
        row = conn.execute(
            "SELECT model_role, provider, exit_code FROM model_invocations WHERE id=?;",
            (result.invocation_id,),
        ).fetchone()
        assert row["model_role"] == "codex"
        assert row["provider"] == "codex"
        assert row["exit_code"] == 0
        # Stdout captured as output file.
        out = (paths.repo_root / result.output_path).read_text(encoding="utf-8")
        assert "verdict: APPROVE" in out
    finally:
        conn.close()


def test_invoke_model_missing_binary_raises_infra(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        with pytest.raises(InfraError):
            invoke_model(
                conn,
                paths,
                events,
                role="planner",
                config={
                    "planner": {
                        "provider": "claude",
                        "command": ["definitely-not-on-path-binary-xyz"],
                        "role": "opus",
                    }
                },
                prompt="hi",
            )
    finally:
        conn.close()


def test_invoke_model_rejects_shell_string(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        with pytest.raises(UsageError):
            invoke_model(
                conn,
                paths,
                events,
                role="planner",
                config={"planner": {"provider": "claude", "command": "claude --model opus"}},
                prompt="hi",
            )
    finally:
        conn.close()


def test_invoke_model_writes_redacted_input(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        fake = tmp_path / "bin" / "fake-claude"
        _write_script(fake, "#!/usr/bin/env bash\nprintf 'ok\\n'\n")
        prompt = "context: api_key=SUPERSECRETKEY\nplease plan"
        os.environ["PATH"] = str(fake.parent) + os.pathsep + os.environ["PATH"]
        try:
            result = invoke_model(
                conn,
                paths,
                events,
                role="planner",
                config={
                    "planner": {
                        "provider": "claude",
                        "command": [str(fake)],
                        "role": "opus",
                    }
                },
                prompt=prompt,
            )
        finally:
            os.environ["PATH"] = os.environ["PATH"].replace(str(fake.parent) + os.pathsep, "")
        text = (paths.repo_root / result.input_path).read_text(encoding="utf-8")
        assert "SUPERSECRETKEY" not in text
        assert "<redacted>" in text
    finally:
        conn.close()


def test_invoke_model_injects_architecture_context(tmp_path: Path) -> None:
    """Issue #293 — the composed prompt carries the architecture block."""
    import shutil

    conn, paths, events = _runtime(tmp_path)
    try:
        # Provide the canonical doc under the tmp repo so injection has a source.
        repo_docs = paths.repo_root / "docs"
        repo_docs.mkdir(parents=True, exist_ok=True)
        src = Path(__file__).resolve().parents[1] / "docs" / "architecture.md"
        shutil.copy2(src, repo_docs / "architecture.md")

        fake = tmp_path / "bin" / "fake-claude"
        _write_script(fake, "#!/usr/bin/env bash\nprintf 'ok\\n'\n")
        os.environ["PATH"] = str(fake.parent) + os.pathsep + os.environ["PATH"]
        try:
            result = invoke_model(
                conn,
                paths,
                events,
                role="planner",
                config={
                    "planner": {
                        "provider": "claude",
                        "command": [str(fake)],
                        "role": "opus",
                    }
                },
                prompt="please plan",
            )
        finally:
            os.environ["PATH"] = os.environ["PATH"].replace(str(fake.parent) + os.pathsep, "")
        text = (paths.repo_root / result.input_path).read_text(encoding="utf-8")
        assert "## Architecture context" in text
        assert "work_item" in text
        # The original task prompt still trails the injected context.
        assert "please plan" in text
    finally:
        conn.close()


def test_parse_reviewer_invocation_strict_format(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        fake = tmp_path / "bin" / "fake-codex"
        _write_script(
            fake,
            "#!/usr/bin/env bash\n"
            "if [ \"${1:-}\" = \"--version\" ]; then echo 'fake-codex 1.0'; exit 0; fi\n"
            "printf '%s\\n' '{\"envelope\":{\"schema_version\":\"1.0\",\"provider\":\"codex\",\"provider_version\":\"fake-codex 1.0\",\"role\":\"reviewer\",\"verdict\":\"REJECT\",\"reason\":\"assertion_weakened\",\"citations\":[{\"file\":\"app.py\",\"line\":1,\"kind\":\"finding\"}],\"body\":\"blocked\",\"metadata\":{\"tokens_in\":10,\"tokens_out\":5}}}'\n",
        )
        os.environ["PATH"] = str(fake.parent) + os.pathsep + os.environ["PATH"]
        try:
            res = invoke_model(
                conn,
                paths,
                events,
                role="reviewer",
                config={
                    "reviewer": {
                        "provider": "codex",
                        "command": [str(fake)],
                        "role": "codex",
                    }
                },
                prompt="review",
            )
        finally:
            os.environ["PATH"] = os.environ["PATH"].replace(str(fake.parent) + os.pathsep, "")
        gate = parse_reviewer_invocation(res, paths)
        assert gate.verdict == "REJECT"
        assert gate.reason == "assertion_weakened"
    finally:
        conn.close()


def test_parse_reviewer_invocation_malformed_raises(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        fake = tmp_path / "bin" / "fake-codex-bad"
        _write_script(fake, "#!/usr/bin/env bash\nprintf 'unstructured slop\\n'\n")
        os.environ["PATH"] = str(fake.parent) + os.pathsep + os.environ["PATH"]
        try:
            res = invoke_model(
                conn,
                paths,
                events,
                role="reviewer",
                config={
                    "reviewer": {
                        "provider": "codex",
                        "command": [str(fake)],
                        "role": "codex",
                    }
                },
                prompt="review",
            )
        finally:
            os.environ["PATH"] = os.environ["PATH"].replace(str(fake.parent) + os.pathsep, "")
        gate = parse_reviewer_invocation(res, paths)
        assert gate.verdict == "REJECT"
        assert gate.reason == "envelope_invalid"
    finally:
        conn.close()


def test_invoke_model_rejects_unknown_role(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        with pytest.raises(UsageError):
            invoke_model(
                conn,
                paths,
                events,
                role="hallucinator",
                config={},
                prompt="hi",
            )
    finally:
        conn.close()
