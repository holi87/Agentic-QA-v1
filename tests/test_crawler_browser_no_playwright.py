"""Verify the missing-Playwright path raises an actionable error.

This test runs without the ``browser`` marker because it must execute
in the default CI matrix where Playwright is *not* installed. We hide
the Playwright import by stubbing ``sys.modules['playwright.sync_api']
= None``, which makes the next ``from playwright.sync_api import ...``
inside :func:`enrich_with_browser_signals` raise ``ImportError`` — the
exact path the helper handles by translating to
``PlaywrightUnavailable`` with install instructions.
"""
from __future__ import annotations

import sys

import pytest

from agentic_os.crawler import CrawlReport
from agentic_os.crawler_browser import (
    PlaywrightUnavailable,
    enrich_with_browser_signals,
)


def test_playwright_unavailable_raises_with_install_hint(monkeypatch):
    real_pw_sync = sys.modules.pop("playwright.sync_api", None)
    real_pw_pkg = sys.modules.pop("playwright", None)
    sys.modules["playwright.sync_api"] = None  # type: ignore[assignment]
    try:
        empty = CrawlReport(
            start_url="http://x/",
            origin="http://x:80",
            user_agent="ua",
            max_pages=1,
            max_depth=0,
            respect_robots=False,
            allow_private=True,
            pages_visited=0,
            pages_skipped_robots=0,
            pages_skipped_off_origin=0,
        )
        with pytest.raises(PlaywrightUnavailable, match="playwright install"):
            enrich_with_browser_signals(empty)
    finally:
        sys.modules.pop("playwright.sync_api", None)
        if real_pw_sync is not None:
            sys.modules["playwright.sync_api"] = real_pw_sync
        if real_pw_pkg is not None:
            sys.modules["playwright"] = real_pw_pkg
