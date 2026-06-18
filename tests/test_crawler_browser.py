"""Tests for in-browser crawler enrichment (issue #156).

A tmp HTTP server serves pages that deliberately emit
``console.error``, ``throw`` from inline JS, and fail a ``fetch()``
call. The test asserts that ``enrich_with_browser_signals`` captures
each signal on the matching route.

Marked ``browser`` so the default ``selftest`` job skips it; the
dedicated screenshots CI job already installs Playwright + Chromium
and will pick this file up.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Tuple

import pytest

from agentic_os.crawler import crawl_same_origin

pytestmark = pytest.mark.browser

pytest.importorskip("playwright.sync_api")
from agentic_os.crawler_browser import (  # noqa: E402
    PlaywrightUnavailable,
    enrich_with_browser_signals,
)


PAGES: Dict[str, Tuple[int, str, str]] = {
    "/": (
        200,
        "text/html",
        """<!doctype html><html><body>
        <a href="/console-err">console.error page</a>
        <a href="/throws">page throws</a>
        <a href="/failed-fetch">failed fetch</a>
        </body></html>""",
    ),
    "/console-err": (
        200,
        "text/html",
        """<!doctype html><html><body>
        <p>has a console.error</p>
        <script>console.error('regression: undefined config')</script>
        </body></html>""",
    ),
    "/throws": (
        200,
        "text/html",
        """<!doctype html><html><body>
        <p>throws on load</p>
        <script>throw new Error('boom from inline script');</script>
        </body></html>""",
    ),
    "/failed-fetch": (
        200,
        "text/html",
        """<!doctype html><html><body>
        <p>fetches a non-existent host</p>
        <script>
          fetch('http://this-host-does-not-resolve.invalid/x').catch(function(){});
        </script>
        </body></html>""",
    ),
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
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

    def log_message(self, *_args, **_kwargs) -> None:  # pragma: no cover
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


def _by_path(report, base: str):
    out = {}
    for r in report.routes:
        out[r.url[len(base):] or "/"] = r
    return out


def test_browser_enrichment_captures_console_error(http_server: str) -> None:
    base = http_server
    report = crawl_same_origin(base + "/", max_depth=1, max_pages=10, allow_private=True)
    enrich_with_browser_signals(report)

    assert report.browser_enriched is True
    route = _by_path(report, base)["/console-err"]
    assert any(
        "regression: undefined config" in c.text and c.level == "error"
        for c in route.console_errors
    ), [c for c in route.console_errors]


def test_browser_enrichment_captures_page_error(http_server: str) -> None:
    base = http_server
    report = crawl_same_origin(base + "/", max_depth=1, max_pages=10, allow_private=True)
    enrich_with_browser_signals(report)

    route = _by_path(report, base)["/throws"]
    assert any("boom from inline script" in p.message for p in route.page_errors), \
        [p.message for p in route.page_errors]


def test_browser_enrichment_captures_failed_request(http_server: str) -> None:
    base = http_server
    report = crawl_same_origin(base + "/", max_depth=1, max_pages=10, allow_private=True)
    enrich_with_browser_signals(report)

    route = _by_path(report, base)["/failed-fetch"]
    matched = [
        r for r in route.failed_requests
        if "this-host-does-not-resolve.invalid" in r.url
    ]
    assert matched, [r.url for r in route.failed_requests]
    assert matched[0].failure  # non-empty failure reason


def test_browser_enrichment_skips_unreachable_routes(http_server: str) -> None:
    """Routes the HTTP crawl couldn't reach (status None or >=400) are not
    re-attempted in the browser — opening a tab at a 5xx adds no signal."""
    base = http_server
    report = crawl_same_origin(base + "/", max_depth=1, max_pages=10, allow_private=True)
    # Inject a synthetic failed route to ensure it is skipped.
    from agentic_os.crawler import CrawledRoute

    report.routes.append(
        CrawledRoute(
            url=base + "/never",
            depth=1,
            status=None,
            content_type=None,
            elapsed_ms=0,
            links_total=0,
            links_internal=0,
            links_external=0,
            error="synthetic",
        )
    )
    enrich_with_browser_signals(report)

    synthetic = next(r for r in report.routes if r.url.endswith("/never"))
    assert synthetic.console_errors == []
    assert synthetic.page_errors == []
    assert synthetic.failed_requests == []


def test_browser_enrichment_serializes_into_json(http_server: str) -> None:
    """``crawl_report_to_json`` includes the new browser signal fields
    and the summary roll-up counts them."""
    from agentic_os.crawler import crawl_report_to_json

    base = http_server
    report = crawl_same_origin(base + "/", max_depth=1, max_pages=10, allow_private=True)
    enrich_with_browser_signals(report)
    payload = crawl_report_to_json(report)

    assert payload["browser_enriched"] is True
    summary = payload["summary"]
    assert summary["console_errors_total"] >= 1
    assert summary["page_errors_total"] >= 1
    assert summary["failed_requests_total"] >= 1
    for route in payload["routes"]:
        assert "console_errors" in route
        assert "page_errors" in route
        assert "failed_requests" in route
