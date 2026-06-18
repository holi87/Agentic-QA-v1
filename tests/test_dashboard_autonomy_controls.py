"""Dashboard autonomy control elements, JavaScript wiring, and budget visual state contracts."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "scripts" / "agentic-os" / "templates"
STATIC = TEMPLATES / "static"


def test_index_html_has_control_elements() -> None:
    html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    for el in (
        'id="autonomy-pause-btn"',
        'id="autonomy-resume-btn"',
        'id="autonomy-blocked-chip"',
        'id="autonomy-budget"',
        'id="autonomy-providers"',
        'id="autonomy-coverage"',
        'id="autonomy-sparklines"',
    ):
        assert el in html, f"index.html missing {el}"


def test_dashboard_js_wires_controls_and_endpoints() -> None:
    js = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    for token in (
        "'/api/autonomy/' + action",
        "sendControl('pause'",
        "sendControl('resume'",
        "/api/budget/status",
        "/api/providers/cooldowns",
        "renderAutonomyWidgets",
        "sess.paused_reason",
        "budget-orange",
        "budget-red",
        "chip-pulse",
    ):
        assert token in js, f"dashboard.js missing {token}"


def test_budget_threshold_colors_present_in_css() -> None:
    css = (STATIC / "dashboard.css").read_text(encoding="utf-8")
    assert ".budget-fill.budget-orange" in css
    assert ".budget-fill.budget-red" in css
    assert ".coverage-donut" in css
    assert ".spark-bars" in css
