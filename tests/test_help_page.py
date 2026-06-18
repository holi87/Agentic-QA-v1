"""Dashboard help wizard — markdown renderer + /help endpoint (issue #50)."""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

from agentic_os.help_md import render
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db
from tests.test_dashboard_task_ui import _DEFAULT_CONFIG, _free_port, _wait


def _runtime(tmp_path: Path) -> RuntimePaths:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    cfg = repo / ".qualitycat" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_DEFAULT_CONFIG.format(write="false").lstrip(), encoding="utf-8")
    conn = init_db(paths.db)
    conn.close()
    return paths


def test_render_headings_get_slug_ids() -> None:
    out = render("# First section\n\nbody\n\n## Sub-Section A\n")
    assert '<h1 id="first-section">' in out
    assert '<h2 id="sub-section-a">' in out
    assert 'href="#first-section"' in out


def test_render_duplicate_headings_get_suffixed_slugs() -> None:
    out = render("## A\n\n## A\n")
    assert '<h2 id="a">' in out
    assert '<h2 id="a-2">' in out


def test_render_lists_paragraphs_and_inline() -> None:
    out = render(
        "Plain **bold** and `code` plus [link](/help).\n\n"
        "- one\n- two\n\n"
        "1. first\n2. second\n"
    )
    assert "<p>Plain <strong>bold</strong> and <code>code</code> plus "\
           '<a href="/help">link</a>.</p>' in out
    assert "<ul><li>one</li><li>two</li></ul>" in out
    assert "<ol><li>first</li><li>second</li></ol>" in out


def test_render_fenced_code_block_is_escaped() -> None:
    src = "```bash\necho '<script>alert(1)</script>'\n```\n"
    out = render(src)
    assert '<pre><code class="lang-bash">' in out
    assert "&lt;script&gt;" in out
    assert "<script>" not in out


def test_render_inline_text_escapes_html() -> None:
    out = render("hostile <img onerror=x> in body")
    assert "&lt;img onerror=x&gt;" in out
    assert "<img" not in out


def test_help_endpoint_renders_dashboard_help_markdown(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    # Copy the real source into the test repo so the server can read it.
    repo_root = Path(__file__).resolve().parents[1]
    source = repo_root / "docs" / "dashboard-help.md"
    assert source.is_file(), "docs/dashboard-help.md is expected to ship with the repo"
    target = paths.repo_root / "docs" / "dashboard-help.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        _wait(base + "/healthz", timeout=5).read()
        with urllib.request.urlopen(base + "/api/help", timeout=4) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["source"] == "docs/dashboard-help.md"
        body = payload["html"]
        # Six required sections from the issue acceptance criteria.
        for slug in (
            "first-run-checklist",
            "task-lifecycle",
            "inbox-quick-start",
            "full-autonomy-primer",
            "troubleshooting",
            "legend",
        ):
            assert f'id="{slug}"' in body, slug
        # Help template + nav link should be reachable too.
        with urllib.request.urlopen(base + "/help", timeout=4) as resp:
            html = resp.read().decode("utf-8")
        assert 'AgenticOS.renderHelpDoc' in html
        assert 'href="/help"' in html
    finally:
        srv._shutdown_requested = True
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def test_help_endpoint_returns_404_when_source_missing(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        _wait(base + "/healthz", timeout=5).read()
        try:
            urllib.request.urlopen(base + "/api/help", timeout=4)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            body = json.loads(exc.read().decode("utf-8"))
            assert body["error"] == "help_doc_missing"
        else:
            raise AssertionError("expected 404 when help source missing")
    finally:
        srv._shutdown_requested = True
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def test_render_keeps_wrapped_list_items_in_single_li() -> None:
    """Multi-line list items must not split into separate <li> entries
    (Codex review #52 — `docs/dashboard-help.md` wraps items across lines)."""
    src = (
        "1. **Pick a SUT.** Edit `.qualitycat/agentic-os.yml` — set\n"
        "   `sut.root`, the URLs / compose file you want.\n"
        "2. **Decide on writes.** Three unlock paths:\n"
        "   - flip the YAML flag,\n"
        "   - restart with `serve --full`,\n"
        "   - start a full autonomy session.\n"
        "3. Done.\n"
    )
    out = render(src)
    # Exactly three top-level ordered items.
    assert out.count("<li>") - out.count("<li>flip") - out.count("<li>restart") - out.count("<li>start") == 3
    # Wrapped second sentence stays in the first item.
    assert "Pick a SUT" in out and "compose file you want" in out
    item_one_end = out.index("</li>")
    assert "compose file you want" in out[:item_one_end]
    # Nested bullets render as a real <ul> under item 2.
    assert "<ul><li>flip the YAML flag,</li>" in out
    assert "<li>restart with <code>serve --full</code>,</li>" in out
    assert "<li>start a full autonomy session.</li></ul>" in out
    # Numbering does not get split (only one <ol>).
    assert out.count("<ol>") == 1


def test_docs_route_renders_whitelisted_markdown(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]
    source = repo_root / "docs" / "troubleshooting.md"
    assert source.is_file(), "docs/troubleshooting.md is expected to ship"
    target = paths.repo_root / "docs" / "troubleshooting.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        _wait(base + "/healthz", timeout=5).read()
        with urllib.request.urlopen(base + "/docs/troubleshooting.md", timeout=4) as resp:
            assert resp.headers.get("Content-Type", "").startswith("text/html")
            body = resp.read().decode("utf-8")
        assert "<h1>" in body
        assert "help-doc" in body
        # Path traversal / outside-whitelist must be 404.
        try:
            urllib.request.urlopen(base + "/docs/../README.md", timeout=4)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        try:
            urllib.request.urlopen(base + "/docs/cli-contract.md", timeout=4)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        srv._shutdown_requested = True
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def test_all_dashboard_templates_link_to_help() -> None:
    """Issue #50 acceptance — Help link on every primary page.

    Issue #296 centralized the nav: shell templates no longer hand-write
    their own links, they carry the ``<!-- DASHBOARD_NAV -->`` sentinel and
    the server injects the canonical nav (which includes Help) on render.
    So the contract is now "every shell template uses the sentinel" plus
    "the canonical nav exposes /help", not a per-file string match.
    """
    from agentic_os.routes.dashboard_server import NAV_LINKS

    assert any(href == "/help" for _, href in NAV_LINKS), "canonical nav lost /help"

    templates = Path(__file__).resolve().parents[1] / "scripts" / "agentic-os" / "templates"
    for name in (
        "index.html",
        "tasks_list.html",
        "tasks_new.html",
        "tasks_detail.html",
        "agents.html",
        "skills.html",
    ):
        html = (templates / name).read_text(encoding="utf-8")
        assert "<!-- DASHBOARD_NAV -->" in html, f"{name} missing nav sentinel"
