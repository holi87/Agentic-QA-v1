"""Issue #373 — the how-to-run guide links report + evidence + bug files.

Acceptance: an operator opens the guide and reaches the report, evidence, and
bug files WITHOUT touching internal runtime paths. The guide therefore carries
clickable (relative) links to the operator-handoff locations, not just text
mentions. (Copying those artifacts into the output volume is A4/#354, already on
main; this issue surfaces + links them.)
"""
from __future__ import annotations

from agentic_os.run_guide import render_run_guide_html


def test_guide_has_clickable_links_to_report_evidence_bugs() -> None:
    html = render_run_guide_html()
    assert "<a href=" in html  # clickable, not just <code> text
    # The three operator-handoff destinations are reachable from the guide.
    assert 'href="reports' in html       # JUnit / HTML report handoff
    assert 'href="evidence' in html      # traces / screenshots / video
    assert 'href="bugs' in html          # BUG-NNN-*.md files


def test_guide_links_the_html_report() -> None:
    html = render_run_guide_html()
    # A direct link to the rendered HTML report (not just the reports/ dir).
    assert "index.html" in html


def test_report_link_is_overridable() -> None:
    html = render_run_guide_html(values={"bugs_dir": "handoff/bugs/"})
    assert 'href="handoff/bugs/"' in html
