"""Issue #372 — CLI command to (re)generate the HTML run guide.

`agentic-os reports html [--output DIR]` regenerates `how-to-run.html` from the
`templates/how-to-run.html.template` (issue #371's guide), filling values from
the project config when present. Standalone (no run needed) and idempotent.
"""
from __future__ import annotations

from pathlib import Path

from agentic_os.cli.cmd_reporting import cmd_reports


def test_reports_html_regenerates_guide(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out"
    rc = cmd_reports(repo, ["html", "--output", str(out)], json_output=False)
    assert rc == 0
    guide = out / "how-to-run.html"
    assert guide.exists()
    body = guide.read_text(encoding="utf-8")
    assert body.lstrip().startswith("<!DOCTYPE html>")
    # The regenerated guide carries the same contract as the bundled one.
    assert "API_BASE_URL" in body
    assert "./run-tests.sh" in body
    assert "mvn" not in body


def test_reports_html_is_idempotent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out"
    cmd_reports(repo, ["html", "--output", str(out)], json_output=False)
    first = (out / "how-to-run.html").read_text(encoding="utf-8")
    cmd_reports(repo, ["html", "--output", str(out)], json_output=False)
    second = (out / "how-to-run.html").read_text(encoding="utf-8")
    assert first == second  # re-running rewrites identical bytes


def test_reports_html_defaults_output_to_repo_output_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = cmd_reports(repo, ["html"], json_output=False)
    assert rc == 0
    assert (repo / "output" / "how-to-run.html").exists()
