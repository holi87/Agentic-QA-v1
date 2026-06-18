"""Issue #371 — final agentic step emits a human-readable how-to-run guide.

The standalone bundle (#369) is the operator handoff artifact a non-author runs.
This guide ships *inside* that bundle as `how-to-run.html`: prerequisites, the
exact commands, the env-var contract (#370), what pass/fail means, and where
reports / evidence / bug files land. Self-contained — inline styling, no
external assets, readable offline. (CLI regeneration is #372; linking the live
report/evidence is #373.)
"""
from __future__ import annotations

import json
from pathlib import Path

from agentic_os.run_guide import render_run_guide_html
from agentic_os.standalone import assemble_standalone_framework


def test_guide_is_self_contained_html() -> None:
    html = render_run_guide_html()
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "<style>" in html  # inline styling
    # Offline-readable: no external stylesheets / scripts / CDN assets.
    assert "<link" not in html
    assert 'src="http' not in html
    assert "cdn" not in html.lower()
    # PL-first per repo audience.
    assert 'lang="pl"' in html


def test_guide_covers_commands_env_and_artifacts() -> None:
    html = render_run_guide_html()
    # npm/Playwright commands — never the stale Java `mvn`.
    assert "./run-tests.sh" in html
    assert "mvn" not in html
    # The two-mode env contract (#370).
    assert "API_BASE_URL" in html
    assert "UI_BASE_URL" in html
    # Where the operator finds outputs + how to read a bug file.
    lower = html.lower()
    assert "reports" in lower
    assert "evidence" in lower
    assert "BUG-" in html


def test_assembled_bundle_includes_the_guide(tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    manifest = assemble_standalone_framework(output_dir=out, tests=[])
    guide = out / "how-to-run.html"
    assert guide.exists(), "the bundle must ship how-to-run.html"
    assert guide.read_text(encoding="utf-8").lstrip().startswith("<!DOCTYPE html>")
    # Discoverable from the manifest.
    assert "how-to-run.html" in json.dumps(manifest)
