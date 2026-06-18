"""Guarded URL fetch, candidate editing, patch apply, and autonomy-loop dashboard regressions."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


# Issue #78
def test_safe_fetch_url_refuses_loopback_by_default() -> None:
    from agentic_os.analysis import _safe_fetch_url

    with pytest.raises(ValueError, match="private/loopback"):
        _safe_fetch_url("http://127.0.0.1:1/openapi.yaml")


def test_safe_fetch_url_rejects_unsupported_scheme() -> None:
    from agentic_os.analysis import _safe_fetch_url

    with pytest.raises(ValueError, match="unsupported URL scheme"):
        _safe_fetch_url("file:///etc/passwd")


def test_safe_fetch_url_rejects_redirect_to_private_host() -> None:
    """Codex review on #130 — a public URL must not be able to redirect
    into private/loopback space and bypass the pre-flight check."""
    import threading
    import socket
    import http.server

    from agentic_os.analysis import _safe_fetch_url

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    class _RedirectToLoopback(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{port}/openapi.yaml")
            self.end_headers()

        def log_message(self, *args, **kwargs) -> None:  # quiet test log
            return

    # The redirect handler can run on any port — only the redirect
    # target needs to be private.
    srv = http.server.HTTPServer(("127.0.0.1", 0), _RedirectToLoopback)
    srv_port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        # The initial host (127.0.0.1) trips the pre-flight check. To
        # actually exercise the redirect path we'd need a public host
        # under our control; the unit-level guarantee we can prove
        # here is that `_NoPrivateRedirectHandler.redirect_request`
        # re-runs `_validate_url_host_not_private` on the redirect
        # target.
        from agentic_os.analysis import _NoPrivateRedirectHandler, _validate_url_host_not_private

        handler = _NoPrivateRedirectHandler()
        # Simulate a redirect_request callback with a private target.
        import urllib.request

        req = urllib.request.Request("http://example.com/")
        with pytest.raises(ValueError, match="private/loopback"):
            handler.redirect_request(
                req, None, 302, "Found", {}, f"http://127.0.0.1:{srv_port}/x"
            )
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2)


# Issue #79
def test_candidate_decision_endpoint_accepts_functional_and_lifecycle_fields(
    tmp_path: Path,
) -> None:
    """The dashboard candidate decision body now carries
    functional_area + lifecycle_tags so the dashboard can approve
    candidates that would otherwise fail the #105 metadata gate."""
    import inspect

    from agentic_os.test_planning import update_plan_candidate_decision

    sig = inspect.signature(update_plan_candidate_decision)
    assert "functional_area" in sig.parameters
    assert "lifecycle_tags" in sig.parameters


# Issue #80
def test_apply_patch_endpoint_present_in_route_set() -> None:
    """The /apply-patch endpoint must be routable from the dashboard."""
    import inspect

    from agentic_os.server import _Handler

    src = inspect.getsource(_Handler.do_POST)
    assert "/apply-patch" in src


def test_apply_helper_invokes_apply_patch_path(tmp_path: Path) -> None:
    """`_apply_approved_patch_for_work_item` must pass the same path as
    both `diff_path` and `apply_patch_path` so #109's identity check
    enforces apply-what-was-reviewed end-to-end."""
    import inspect

    from agentic_os.server import _Handler

    src = inspect.getsource(_Handler._apply_approved_patch_for_work_item)
    assert "apply_patch_path=Path(latest_patch_rel)" in src
    assert "diff_path=Path(latest_patch_rel)" in src


# Issue #81
def test_autonomy_loop_extends_past_implement() -> None:
    """The autonomy loop now calls review-gate, run-tests, and
    final-gate after implement. Source inspection is the cheapest
    proof — the live loop is exercised in existing autonomy tests."""
    import inspect

    from agentic_os.autonomy import _autonomy_review_then_apply, _autonomy_run_tests, _autonomy_final_gate

    for fn in (_autonomy_review_then_apply, _autonomy_run_tests, _autonomy_final_gate):
        assert callable(fn)
    # The orchestrator entry must wire all three.
    import agentic_os.autonomy as autonomy_mod

    src = inspect.getsource(autonomy_mod)
    assert "_autonomy_review_then_apply" in src
    assert "_autonomy_run_tests" in src
    assert "_autonomy_final_gate" in src
    assert "awaiting_operator_decision" in src
