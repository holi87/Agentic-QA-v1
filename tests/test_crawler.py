"""Tests for the same-origin crawler (issue #136).

A tmp ``ThreadingHTTPServer`` serves a small HTML graph the test
asserts the BFS walks correctly: depth limit, max-pages cap, robots
.txt obedience, off-origin link rejection, and broken-asset detection
on a 404 image.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Tuple

import pytest

from agentic_os.crawler import (
    CrawlReport,
    crawl_report_to_json,
    crawl_same_origin,
)


# ---------------------------------------------------------------------------
# Tmp HTTP server harness
# ---------------------------------------------------------------------------


PAGES: Dict[str, Tuple[int, str, str]] = {
    # path -> (status, content_type, body)
    "/": (
        200,
        "text/html",
        """<!doctype html><html><body>
        <a href="/about">About</a>
        <a href="/blog">Blog index</a>
        <a href="https://external.example.test/">External</a>
        <img src="/static/logo.png">
        <img src="/static/missing.png">
        </body></html>""",
    ),
    "/about": (
        200,
        "text/html",
        """<!doctype html><html><body>
        <a href="/contact">Contact</a>
        <a href="/">Home</a>
        </body></html>""",
    ),
    "/contact": (
        200,
        "text/html",
        """<!doctype html><html><body><a href="/">Home</a></body></html>""",
    ),
    "/blog": (
        200,
        "text/html",
        """<!doctype html><html><body>
        <a href="/blog/post-1">Post 1</a>
        <a href="/blog/post-2">Post 2</a>
        </body></html>""",
    ),
    "/blog/post-1": (
        200,
        "text/html",
        """<!doctype html><html><body><a href="/blog">Back</a></body></html>""",
    ),
    "/blog/post-2": (
        200,
        "text/html",
        """<!doctype html><html><body><a href="/blog">Back</a></body></html>""",
    ),
    "/static/logo.png": (200, "image/png", "PNGBYTES"),
    "/disallowed": (
        200,
        "text/html",
        """<!doctype html><html><body>secret</body></html>""",
    ),
}

ROBOTS = "User-agent: *\nDisallow: /disallowed\n"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/robots.txt":
            body = ROBOTS.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        page = PAGES.get(self.path)
        if page is None:
            self.send_error(404, "not found")
            return
        status, ctype, body = page
        body_bytes = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def do_HEAD(self) -> None:  # noqa: N802
        if self.path == "/robots.txt":
            self.send_response(200)
            self.end_headers()
            return
        page = PAGES.get(self.path)
        if page is None:
            self.send_error(404, "not found")
            return
        status, ctype, _ = page
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.end_headers()

    def log_message(self, *_args, **_kwargs) -> None:  # pragma: no cover
        # Quiet test output — the harness asserts behavior, not access log.
        return


@pytest.fixture
def http_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield base
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _by_path(report: CrawlReport, base: str) -> Dict[str, dict]:
    payload = crawl_report_to_json(report)
    out = {}
    for r in payload["routes"]:
        # Trim the http://host:port prefix so the assertions read as
        # site paths regardless of the randomly bound port.
        path = r["url"][len(base) :] or "/"
        out[path] = r
    return out


def test_crawl_visits_same_origin_routes_within_depth(http_server: str) -> None:
    base = http_server
    report = crawl_same_origin(
        base + "/", max_depth=2, max_pages=25, allow_private=True
    )

    paths = set(_by_path(report, base).keys())
    # depth 0: "/"
    # depth 1: /about, /blog (and /disallowed would be here but robots blocks it)
    # depth 2: /contact, /blog/post-1, /blog/post-2
    assert paths >= {"/", "/about", "/blog", "/contact", "/blog/post-1", "/blog/post-2"}


def test_crawl_respects_depth_zero(http_server: str) -> None:
    base = http_server
    report = crawl_same_origin(
        base + "/", max_depth=0, max_pages=25, allow_private=True
    )
    assert report.pages_visited == 1
    assert report.routes[0].url == base + "/"
    # No descendant URLs were enqueued.
    paths = set(_by_path(report, base).keys())
    assert paths == {"/"}


def test_crawl_respects_max_pages(http_server: str) -> None:
    base = http_server
    report = crawl_same_origin(
        base + "/", max_depth=5, max_pages=3, allow_private=True
    )
    assert report.pages_visited == 3
    assert len(report.routes) == 3


def test_crawl_respects_robots_txt(http_server: str) -> None:
    base = http_server
    # Sanity: when robots is honored, /disallowed never appears.
    report = crawl_same_origin(
        base + "/disallowed", max_depth=0, max_pages=5, allow_private=True
    )
    assert report.pages_skipped_robots == 1
    assert report.pages_visited == 0

    # And when ignored, the page IS visited.
    bypass = crawl_same_origin(
        base + "/disallowed",
        max_depth=0,
        max_pages=5,
        respect_robots=False,
        allow_private=True,
    )
    assert bypass.pages_visited == 1
    assert bypass.routes[0].status == 200


def test_crawl_records_broken_assets(http_server: str) -> None:
    base = http_server
    report = crawl_same_origin(
        base + "/", max_depth=0, max_pages=5, allow_private=True
    )
    home = _by_path(report, base)["/"]
    broken_urls = {b["url"] for b in home["broken_assets"]}
    # /static/missing.png returns 404 → must show up.
    assert any(u.endswith("/static/missing.png") for u in broken_urls), broken_urls
    # /static/logo.png returns 200 → must NOT show up.
    assert not any(u.endswith("/static/logo.png") for u in broken_urls)


def test_crawl_does_not_follow_off_origin_links(http_server: str) -> None:
    base = http_server
    report = crawl_same_origin(
        base + "/", max_depth=3, max_pages=25, allow_private=True
    )
    payload = crawl_report_to_json(report)
    # The external host must never appear as a fetched route — only
    # as an "external" link counter on the home page.
    external_origin = "external.example.test"
    for route in payload["routes"]:
        assert external_origin not in route["url"], route["url"]
    home = _by_path(report, base)["/"]
    assert home["links_external"] >= 1


def test_crawl_rejects_private_host_by_default() -> None:
    # 127.0.0.1 with allow_private=False (the public-internet default)
    # must be refused — otherwise the CLI is an SSRF vector when an
    # operator passes a malicious task spec.
    with pytest.raises(ValueError, match="loopback|private|link-local"):
        crawl_same_origin("http://127.0.0.1/", max_depth=0, max_pages=1)


def test_crawl_report_summary_counts_broken_assets(http_server: str) -> None:
    base = http_server
    report = crawl_same_origin(
        base + "/", max_depth=1, max_pages=10, allow_private=True
    )
    payload = crawl_report_to_json(report)
    assert payload["summary"]["pages_visited"] == report.pages_visited
    assert payload["summary"]["broken_assets_total"] >= 1
    assert payload["summary"]["pages_skipped_robots"] == report.pages_skipped_robots
