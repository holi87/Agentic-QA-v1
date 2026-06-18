from __future__ import annotations

import http.client
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "agentic-os"))

# Issue #291 — pin a deterministic dashboard unsafe-method token so every
# server constructed during the suite accepts the token the autouse fixture
# below injects. Set at import time (before any fixture or server build).
DASHBOARD_TEST_TOKEN = "test-dashboard-token"
os.environ.setdefault("AGENTIC_DASHBOARD_TOKEN", DASHBOARD_TEST_TOKEN)

_UNSAFE_METHODS = {"POST", "PUT", "DELETE"}
_orig_http_request = http.client.HTTPConnection.request


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "no_dashboard_token: do not auto-inject the X-Agentic-Token header "
        "(for tests that assert unauthenticated unsafe requests are rejected)",
    )


@pytest.fixture(autouse=True)
def _inject_dashboard_token(request: pytest.FixtureRequest, monkeypatch):
    """Attach the dashboard token to unsafe HTTP requests in tests.

    Existing tests issue POST/PUT/DELETE via `urllib`/`http.client` without
    the auth header introduced in issue #291. Rather than edit every call
    site, wrap `HTTPConnection.request` (which both urllib and http.client
    funnel through) to add `X-Agentic-Token` when the caller did not set one.

    Tests marked `no_dashboard_token` opt out so they can assert that an
    unauthenticated unsafe request is rejected.
    """
    if request.node.get_closest_marker("no_dashboard_token"):
        return

    def _patched(self, method, url, body=None, headers=None, *, encode_chunked=False):
        headers = dict(headers or {})
        if str(method).upper() in _UNSAFE_METHODS and "X-Agentic-Token" not in headers:
            headers["X-Agentic-Token"] = os.environ.get(
                "AGENTIC_DASHBOARD_TOKEN", DASHBOARD_TEST_TOKEN
            )
        return _orig_http_request(
            self, method, url, body=body, headers=headers, encode_chunked=encode_chunked
        )

    monkeypatch.setattr(http.client.HTTPConnection, "request", _patched)
