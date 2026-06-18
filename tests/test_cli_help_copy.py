"""CLI help text + error message copy. Closes #57, #58."""
from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agentic_os.cli import HELP_TEXT, main as cli_main


def test_help_text_documents_serve_alias_with_full_flag() -> None:
    """Issue #58 — the dashboard help / warning copy refers to
    `serve --full`. HELP_TEXT must document that the alias accepts the
    same flags as the target command so operators see it as a first-class
    invocation rather than undocumented coupling."""
    assert "serve [...flags]" in HELP_TEXT
    assert "serve --full" in HELP_TEXT
    assert "extra flags pass through" in HELP_TEXT.lower()


def test_init_missing_template_error_names_canonical_path_only(tmp_path: Path) -> None:
    """Issue #57 — the init error should not advertise the legacy
    `.qualitycat/agentic-os.yml.example` as a recovery path. New
    operators should be pointed at the canonical config template plus a
    concrete recovery step."""
    # Make a repo with the agentic-os.sh shim but no config template
    # anywhere — `init` should fail with the cleaned-up error.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "scripts").mkdir()
    (repo / "scripts" / "agentic-os.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / ".agentic-os").mkdir()

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main(["--root", str(repo), "init"])
    message = err.getvalue()
    assert rc != 0
    assert "config/agentic-os.yml.example" in message
    assert ".qualitycat" not in message
    assert "git checkout" in message
