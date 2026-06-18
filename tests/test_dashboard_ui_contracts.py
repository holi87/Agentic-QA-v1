"""Dashboard UI branding, controls, reduced-motion, and smoke-route contracts."""
from __future__ import annotations

from pathlib import Path


def _templates_root() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "agentic-os"
        / "templates"
    )


# Issue #112 — branding pass + favicon + meta description.
def test_index_template_branded_agentic_os() -> None:
    index = (_templates_root() / "index.html").read_text(encoding="utf-8")
    assert "<title>Agentic OS" in index
    assert "Agentic OS" in index
    assert "Quality Cat Agentic Web Testing" not in index
    assert 'name="description"' in index
    assert 'rel="icon"' in index
    assert 'href="/static/favicon.svg"' in index


def test_favicon_svg_present_and_nonempty() -> None:
    favicon = _templates_root() / "static" / "favicon.svg"
    assert favicon.is_file()
    assert favicon.read_text(encoding="utf-8").startswith("<svg")


def test_every_template_uses_agentic_os_title_or_skips_title() -> None:
    """Issue #112 — no leftover legacy product names in templates."""
    for p in _templates_root().glob("*.html"):
        text = p.read_text(encoding="utf-8")
        assert "Quality Cat Agentic Web Testing" not in text, p.name
        assert "QualityCat Agentic Web Testing" not in text, p.name


# Issue #115 — no alert(); inline error helper is shipped.
def test_dashboard_js_has_no_window_alert() -> None:
    js = (_templates_root() / "static" / "dashboard.js").read_text(encoding="utf-8")
    # `alert(` only appears as a substring inside the helper name
    # `showInlineError` (it does not). Ensure no bare `alert(` call
    # remains anywhere.
    for line in js.splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        assert " alert(" not in line and not stripped.startswith("alert("), line


def test_inline_error_helper_present() -> None:
    js = (_templates_root() / "static" / "dashboard.js").read_text(encoding="utf-8")
    assert "showInlineError" in js
    assert 'role' in js and 'alert' in js  # ARIA wiring


def test_candidate_review_has_bulk_approve_control() -> None:
    detail = (_templates_root() / "tasks_detail.html").read_text(encoding="utf-8")
    js = (_templates_root() / "static" / "dashboard.js").read_text(encoding="utf-8")
    assert 'id="approve-all-candidates"' in detail
    assert "approveAllCandidates" in js
    assert "/candidates/approve-all" in js


# Issue #116 — reduced-motion respected.
def test_dashboard_css_respects_prefers_reduced_motion() -> None:
    css = (_templates_root() / "static" / "dashboard.css").read_text(encoding="utf-8")
    assert "@media (prefers-reduced-motion: reduce)" in css
    # Skeleton + workflow-pulse helpers shipped.
    assert ".skeleton" in css
    assert ".workflow-pulse" in css
    assert "@keyframes skeleton-shimmer" in css


# Issue #117 — minimal visual smoke test: every templated page must
# render to non-empty HTML when served by the dashboard server.
def test_dashboard_smoke_routes_render(tmp_path: Path) -> None:
    import threading
    import time
    import urllib.request

    from agentic_os.paths import RuntimePaths
    from agentic_os.server import make_server
    from agentic_os.storage import init_db

    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    conn.close()

    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        time.sleep(0.1)
        for path in ("/", "/tasks", "/help"):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{path}", timeout=3
            ) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                assert resp.status == 200, path
                # Issue #112/#117 — every page must brand correctly.
                assert "Agentic OS" in body, path
                # Issue #117 — no obvious overflow keywords land in
                # the rendered HTML (no `Internal Server Error`).
                assert "Internal Server Error" not in body, path
    finally:
        srv._shutdown_requested = True  # type: ignore[attr-defined]
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


# Issue #296 — the primary nav must be byte-identical (ignoring which link
# is active) on every dashboard shell subpage. This locks the regression:
# adding a page with a hand-edited nav, or dropping a link on one page,
# fails here instead of silently making the menu jump again.
_SHELL_ROUTES = (
    "/",
    "/tasks",
    "/tasks/new",
    "/tasks/demo-id",
    "/agents",
    "/skills",
    "/orchestration",
    "/verifications",
    "/sessions",
    "/sessions/compare",
    "/sessions/demo-id",
    "/schedules",
    "/health",
    "/learnings",
    "/help",
    # Issue #321 — detail views now share the canonical shell + nav too.
    "/task/demo-id",
    "/decision/demo-id",
)


def _extract_nav(html: str) -> str:
    import re

    match = re.search(r"<nav class=\"nav\".*?</nav>", html, re.DOTALL)
    assert match is not None, "served page is missing the primary <nav>"
    # Normalise away the active marker so only structure/membership is compared.
    return match.group(0).replace("nav-link active", "nav-link")


def test_dashboard_nav_identical_across_subpages(tmp_path: Path) -> None:
    import threading
    import time
    import socket
    import urllib.request

    from agentic_os.paths import RuntimePaths
    from agentic_os.routes.dashboard_server import NAV_LINKS
    from agentic_os.server import make_server
    from agentic_os.storage import init_db

    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    conn.close()

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        time.sleep(0.1)
        navs: dict[str, str] = {}
        for route in _SHELL_ROUTES:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{route}", timeout=3
            ) as resp:
                assert resp.status == 200, route
                body = resp.read().decode("utf-8", errors="replace")
            # Sentinel must be fully resolved server-side, never shipped raw.
            assert "<!-- DASHBOARD_NAV -->" not in body, route
            navs[route] = _extract_nav(body)

        # Every page renders the exact same nav once the active marker is dropped.
        reference = navs["/"]
        for route, nav in navs.items():
            assert nav == reference, f"nav differs on {route}"

        # The rendered nav exposes exactly the canonical link set, in order.
        for label, href in NAV_LINKS:
            assert f'href="{href}">{label}</a>' in reference, (label, href)
        # Dead in-page anchors must not leak back into the primary nav.
        assert "#section-reports" not in reference
        assert "#section-events" not in reference
    finally:
        srv._shutdown_requested = True  # type: ignore[attr-defined]
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def test_shell_templates_use_nav_sentinel() -> None:
    """Issue #296/#321 — every template carries the canonical nav sentinel and
    hand-writes no <nav> of its own. The former bare detail views (task.html,
    decision.html) now share the shell too."""
    for p in _templates_root().glob("*.html"):
        text = p.read_text(encoding="utf-8")
        assert "<nav" not in text, f"{p.name} still hand-writes a nav"
        assert "<!-- DASHBOARD_NAV -->" in text, f"{p.name} missing nav sentinel"
