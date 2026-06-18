"""Dashboard cohesion — tokens-only styling, landmark coverage, and the
canonical nav layout (#316).

Pins invariants future template edits cannot regress:

* No template ships an inline ``<style>`` block — every visual rule
  lives in ``dashboard.css`` so the token system is the single source.
* No template ships layout-shape ``style="..."`` attributes — utility
  classes (``.row-flex``, ``.grid-2``, ``.detail-shell``) are required.
  Per-element overrides like ``style="margin-left: auto"`` for an
  individual control are still acceptable.
* Every shell template carries the WAI-ARIA landmarks the screen-
  reader cohort relies on: ``<header role="banner">``, the canonical
  nav sentinel, and a ``<main>`` element.
* The new ``.detail-shell`` class lives in dashboard.css and is the
  one carrying the layout for ``task.html`` / ``decision.html``.
* ``docs/design-tokens.md`` exists so contributors discover the token
  system without code-spelunking.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "scripts" / "agentic-os" / "templates"
CSS_PATH = TEMPLATES_DIR / "static" / "dashboard.css"

# Templates that render full dashboard shells (carry topbar + nav + main).
# Modals/partials live elsewhere and don't need landmarks.
SHELL_TEMPLATES = [
    "index.html",
    "tasks_list.html",
    "tasks_new.html",
    "tasks_detail.html",
    "agents.html",
    "skills.html",
    "orchestration.html",
    "verifications.html",
    "sessions.html",
    "sessions_compare.html",
    "schedules.html",
    "health.html",
    "learnings.html",
    "help.html",
    "metrics_cockpit.html",
    "decision.html",
    "task.html",
]


@pytest.mark.parametrize("name", SHELL_TEMPLATES)
def test_template_has_landmark_topbar_and_main(name: str) -> None:
    html = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    assert 'role="banner"' in html, f"{name}: missing topbar/banner landmark"
    assert "<!-- DASHBOARD_NAV -->" in html, f"{name}: missing canonical nav sentinel"
    assert "<main" in html, f"{name}: missing <main> landmark"


@pytest.mark.parametrize("name", SHELL_TEMPLATES)
def test_template_has_no_inline_style_block(name: str) -> None:
    """Templates must not ship their own <style> block — every rule lives
    in dashboard.css so the token system is the source. Per-page rules
    belong under a namespaced class (see decision.html → .detail-shell)."""
    html = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    assert "<style>" not in html, (
        f"{name}: inline <style> block forbidden. "
        "Move rules into dashboard.css under a namespaced class."
    )


_LAYOUT_STYLE_RE = re.compile(
    r'style="[^"]*(display\s*:|grid-template-columns\s*:|flex-direction\s*:)',
    re.IGNORECASE,
)


@pytest.mark.parametrize("name", SHELL_TEMPLATES)
def test_template_has_no_layout_inline_style(name: str) -> None:
    """Layout-shape inline styles (display/grid-template-columns/flex-
    direction) must move into utility classes — they are reusable enough
    that the dashboard.css cost beats per-template churn."""
    html = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    hits = _LAYOUT_STYLE_RE.findall(html)
    assert not hits, (
        f"{name}: layout-shape inline styles still present ({hits!r}). "
        "Use .row-flex, .grid-2, or add a new utility class to dashboard.css."
    )


def test_detail_shell_css_lives_in_dashboard_css() -> None:
    css = CSS_PATH.read_text(encoding="utf-8")
    assert ".detail-shell" in css, (
        "detail-shell layout must live in dashboard.css so task.html "
        "and decision.html share one source of truth"
    )
    assert ".detail-shell main" in css
    assert ".detail-shell .meta" in css


def test_metrics_cockpit_css_lives_in_dashboard_css() -> None:
    css = CSS_PATH.read_text(encoding="utf-8")
    for token in (".metrics-grid", ".metric-card", ".metric-row", ".metric-empty"):
        assert token in css, f"{token} must live in dashboard.css (Wave 16)"


def test_row_flex_utility_class_present() -> None:
    css = CSS_PATH.read_text(encoding="utf-8")
    assert ".row-flex" in css, ".row-flex utility class must exist in dashboard.css"
    assert ".grid-2" in css, ".grid-2 utility class must exist in dashboard.css"


def test_design_tokens_doc_ships() -> None:
    doc = REPO_ROOT / "docs" / "design-tokens.md"
    assert doc.is_file(), "docs/design-tokens.md must ship so contributors find the token system"
    body = doc.read_text(encoding="utf-8")
    # Quick sanity: the doc references the actual token names so a rename
    # without doc update gets caught by review.
    for token in ("--bg", "--surface", "--primary", "--state-ready", "--text"):
        assert token in body, f"design-tokens.md must reference {token}"


def test_task_and_decision_use_detail_shell_class() -> None:
    for name in ("task.html", "decision.html"):
        html = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        assert '<body class="detail-shell">' in html, (
            f"{name}: must carry <body class=\"detail-shell\"> so the "
            "compact layout from dashboard.css applies"
        )
