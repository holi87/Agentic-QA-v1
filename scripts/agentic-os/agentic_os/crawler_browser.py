"""In-browser enrichment for the HTTP crawler (issue #156).

The stdlib HTTP crawler in :mod:`agentic_os.crawler` is deterministic
and dependency-free but only sees what the HTTP layer exposes — status
codes, broken assets, link graph. Real regressions on a public site
also surface as:

- ``console.error`` messages from runtime JS,
- uncaught exceptions (``page.on('pageerror')``),
- failed XHR/fetch and CSP violations (``page.on('requestfailed')``).

This module visits each already-discovered route in headless Chromium,
listens on those events, and mutates the :class:`CrawlReport` in place
with the captured signals. Playwright is an *opt-in* dependency
(``pip install playwright`` + ``playwright install chromium``); calling
this without Playwright installed raises :class:`PlaywrightUnavailable`
so the operator gets a clear "install this" message rather than an
ImportError mid-crawl.

The enrichment respects the same SSRF guard the HTTP crawler uses
(``_check_private``), so a malicious task spec can't make the browser
reach loopback / RFC1918 hosts even though the browser would happily
follow them otherwise.
"""
from __future__ import annotations

from typing import List, Optional

from .crawler import (
    ConsoleMessage,
    CrawlReport,
    CrawledRoute,
    FailedRequest,
    PageError,
    _check_private,
)


_DEFAULT_NAV_TIMEOUT_MS = 10_000
_DEFAULT_SETTLE_TIMEOUT_MS = 2_000


class PlaywrightUnavailable(RuntimeError):
    """Raised when ``--browser`` is requested but Playwright is missing.

    The message includes the install commands the operator needs, so
    the CLI does not need to know about Playwright's package surface.
    """


def enrich_with_browser_signals(
    report: CrawlReport,
    *,
    nav_timeout_ms: int = _DEFAULT_NAV_TIMEOUT_MS,
    settle_timeout_ms: int = _DEFAULT_SETTLE_TIMEOUT_MS,
    user_agent: Optional[str] = None,
) -> CrawlReport:
    """Visit every successful route in Chromium and capture browser signals.

    Mutates ``report`` in place (and returns it for chaining).

    * Routes that the HTTP crawl could not reach (``status is None`` or
      ``status >= 400``) are skipped — opening a browser at a 5xx adds
      no signal.
    * Routes targeting loopback/private/link-local hosts are skipped
      unless ``report.allow_private`` is true, mirroring the HTTP-crawl
      SSRF guard.
    * Each route runs in a fresh Playwright context so cookies/state
      from one page don't leak into the next. We don't reuse the
      browser to avoid a long-lived shared profile pinning memory.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PlaywrightUnavailable(
            "Playwright is not installed. Install with:\n"
            "  pip install playwright\n"
            "  python -m playwright install chromium"
        ) from exc

    ua = user_agent or report.user_agent

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for route in report.routes:
                if not _route_is_visitable(route, allow_private=report.allow_private):
                    continue
                _capture_route_signals(
                    browser,
                    route,
                    user_agent=ua,
                    nav_timeout_ms=nav_timeout_ms,
                    settle_timeout_ms=settle_timeout_ms,
                )
        finally:
            browser.close()

    report.browser_enriched = True
    return report


def _route_is_visitable(route: CrawledRoute, *, allow_private: bool) -> bool:
    if route.status is None:
        return False
    if route.status >= 400:
        return False
    if _check_private(route.url, allow_private=allow_private) is not None:
        return False
    return True


def _capture_route_signals(
    browser,
    route: CrawledRoute,
    *,
    user_agent: str,
    nav_timeout_ms: int,
    settle_timeout_ms: int,
) -> None:
    """Open a single context, navigate, capture signals into ``route``."""
    console_errors: List[ConsoleMessage] = []
    page_errors: List[PageError] = []
    failed_requests: List[FailedRequest] = []

    context = browser.new_context(user_agent=user_agent)
    try:
        page = context.new_page()

        def on_console(msg) -> None:
            # We only record severity that signals a regression. Logs
            # and debug noise are explicitly excluded so the report
            # stays readable. CSP violations and mixed-content warnings
            # land in ``warning`` per the Chromium emit semantics, which
            # is why we include warnings here.
            kind = (getattr(msg, "type", None) or "").lower()
            if kind not in {"error", "warning"}:
                return
            location = _safe_location(msg)
            console_errors.append(
                ConsoleMessage(
                    level=kind,
                    text=_safe_text(msg),
                    url=location.get("url"),
                    line=location.get("line"),
                )
            )

        def on_pageerror(err) -> None:
            page_errors.append(
                PageError(
                    message=str(getattr(err, "message", None) or err),
                    stack=getattr(err, "stack", None),
                )
            )

        def on_requestfailed(req) -> None:
            failure = getattr(req, "failure", None)
            # Playwright's failure attribute is a callable returning a
            # dict, but the property form ("page.on('requestfailed')")
            # already gives the request; defensively support both shapes.
            failure_text = ""
            if callable(failure):
                try:
                    info = failure() or {}
                    failure_text = (info or {}).get("errorText", "") if isinstance(info, dict) else str(info)
                except Exception:  # noqa: BLE001
                    failure_text = ""
            elif isinstance(failure, dict):
                failure_text = failure.get("errorText", "")
            elif failure is not None:
                failure_text = str(failure)
            failed_requests.append(
                FailedRequest(
                    url=getattr(req, "url", ""),
                    method=getattr(req, "method", "") or "",
                    failure=failure_text or "unknown failure",
                    resource_type=getattr(req, "resource_type", None),
                )
            )

        page.on("console", on_console)
        page.on("pageerror", on_pageerror)
        page.on("requestfailed", on_requestfailed)

        try:
            page.goto(route.url, timeout=nav_timeout_ms, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001 — navigation failure is a signal
            page_errors.append(PageError(message=f"navigation failed: {exc}"))
        else:
            # Best-effort settle so late console.errors / failed XHRs
            # fire before we tear the page down. ``networkidle`` is
            # bounded so a long-poll never blocks the crawl.
            try:
                page.wait_for_load_state("networkidle", timeout=settle_timeout_ms)
            except Exception:  # noqa: BLE001
                pass
    finally:
        try:
            context.close()
        except Exception:  # noqa: BLE001
            pass

    route.console_errors.extend(console_errors)
    route.page_errors.extend(page_errors)
    route.failed_requests.extend(failed_requests)


def _safe_location(msg) -> dict:
    """Pull ``url`` / ``line`` out of a Playwright ConsoleMessage."""
    try:
        loc = msg.location  # may be a property OR a dict, depending on version
    except Exception:  # noqa: BLE001
        return {"url": None, "line": None}
    if not loc:
        return {"url": None, "line": None}
    url = loc.get("url") if isinstance(loc, dict) else getattr(loc, "url", None)
    line = loc.get("lineNumber") if isinstance(loc, dict) else getattr(loc, "lineNumber", None)
    return {"url": url, "line": line}


def _safe_text(msg) -> str:
    try:
        return msg.text
    except Exception:  # noqa: BLE001
        return str(msg)
