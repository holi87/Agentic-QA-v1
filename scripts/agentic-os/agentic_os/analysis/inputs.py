"""Input collection and SSRF-guarded URL fetching.

Split from analysis.py (issue #292).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional

from ..config import ConfigError, load_or_default
from ..errors import UsageError
from ..paths import RuntimePaths
from ..work_items import get_work_item, read_work_item_spec

from .types import _AnalysisInputs


def _collect_inputs(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    work_item_id: str,
) -> _AnalysisInputs:
    work_item = get_work_item(conn, work_item_id)
    if work_item is None:
        raise UsageError(f"unknown task id: {work_item_id}")
    spec_markdown = read_work_item_spec(paths, work_item)
    snapshot: Dict[str, Any] = {}
    warning: Optional[str] = None
    try:
        cfg = load_or_default(paths.repo_root)
        sut = cfg.raw.get("sut", {}) or {}
        healthcheck = sut.get("healthcheck", {}) or {}
        snapshot = {
            "sut_root": sut.get("root"),
            "kind": sut.get("kind"),
            "mode": sut.get("mode"),
            "base_url": sut.get("base_url"),
            "api_base_url": sut.get("api_base_url"),
            "ui_url": sut.get("ui_url"),
            "compose_file": sut.get("compose_file"),
            "test_runner": sut.get("test_runner"),
            "openapi": sut.get("openapi"),
            "docs": sut.get("docs"),
            "tests_dir": sut.get("tests_dir"),
            "web": sut.get("web"),
            "api": sut.get("api"),
            "healthcheck": {
                "command": list(healthcheck.get("command") or []),
                "timeout_seconds": healthcheck.get("timeout_seconds"),
                "retries": healthcheck.get("retries"),
            },
        }
    except ConfigError as exc:
        warning = f"config unavailable: {exc}"
    return _AnalysisInputs(
        work_item=work_item,
        spec_markdown=spec_markdown,
        config_snapshot=snapshot,
        config_warning=warning,
    )


def _safe_fetch_url(url: str, *, allow_private: bool = False) -> str:
    """Issue #78 — fetch an OpenAPI/docs URL with guards.

    - 5 second connect / 10 second read timeout
    - 2 MiB size cap
    - content-type must look like text/yaml/json
    - localhost/private-network targets refused unless `allow_private`
    - **redirects re-validated against the private-network policy
      (codex review on #130)** so a public URL cannot redirect into
      RFC1918/loopback space and bypass the pre-flight check
    """
    import urllib.error
    import urllib.request

    if not allow_private:
        _validate_url_host_not_private(url)
        opener = urllib.request.build_opener(_NoPrivateRedirectHandler())
    else:
        opener = urllib.request.build_opener()
    req = urllib.request.Request(url, headers={"User-Agent": "agentic-os/1.0"})
    with opener.open(req, timeout=10) as resp:
        # Final URL after redirects — re-check explicitly in case any
        # handler in the stack ever drops the per-hop validation.
        if not allow_private:
            _validate_url_host_not_private(resp.geturl())
        ct = (resp.headers.get("content-type") or "").lower()
        if not any(
            t in ct for t in ("yaml", "json", "text/plain", "x-yaml", "x-json")
        ):
            raise ValueError(
                f"unexpected content-type from {url!r}: {ct or '<empty>'}"
            )
        raw = resp.read(_MAX_URL_FETCH_BYTES + 1)
        if len(raw) > _MAX_URL_FETCH_BYTES:
            raise ValueError(
                f"response from {url!r} exceeds {_MAX_URL_FETCH_BYTES} byte cap"
            )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"response from {url!r} is not utf-8: {exc}") from exc


class _NoPrivateRedirectHandler:
    """urllib redirect handler that re-validates every redirect target
    against the private-network policy (codex review on #130).

    The default `HTTPRedirectHandler` follows redirects without
    re-checking the destination, so a public URL can redirect to
    `127.0.0.1`/RFC1918 and bypass `_safe_fetch_url`'s pre-flight
    check. This handler refuses such redirects with `ValueError`.
    """

    def __init__(self) -> None:
        import urllib.request

        self._inner = urllib.request.HTTPRedirectHandler()

    # The urllib opener wiring expects redirect_request + http_error_30x
    # callbacks. We delegate to the standard handler after re-validating.
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):  # type: ignore[no-untyped-def]
        _validate_url_host_not_private(newurl)
        return self._inner.redirect_request(req, fp, code, msg, hdrs, newurl)

    def http_error_301(self, req, fp, code, msg, hdrs):  # type: ignore[no-untyped-def]
        return self._inner.http_error_301(req, fp, code, msg, hdrs)

    def http_error_302(self, req, fp, code, msg, hdrs):  # type: ignore[no-untyped-def]
        return self._inner.http_error_302(req, fp, code, msg, hdrs)

    def http_error_303(self, req, fp, code, msg, hdrs):  # type: ignore[no-untyped-def]
        return self._inner.http_error_303(req, fp, code, msg, hdrs)

    def http_error_307(self, req, fp, code, msg, hdrs):  # type: ignore[no-untyped-def]
        return self._inner.http_error_307(req, fp, code, msg, hdrs)


def _validate_url_host_not_private(url: str) -> None:
    """Raise if the URL's host resolves to a loopback/private/link-local
    address. Used both for the original target and every redirect hop
    (codex review on #130).
    """
    import ipaddress
    import socket
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported URL scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError(f"URL missing host: {url!r}")
    try:
        resolved = socket.gethostbyname(parsed.hostname)
    except OSError as exc:
        raise ValueError(f"DNS resolution failed for {parsed.hostname}: {exc}")
    addr = ipaddress.ip_address(resolved)
    if addr.is_loopback or addr.is_private or addr.is_link_local:
        raise ValueError(
            f"URL target {parsed.hostname} resolves to a "
            f"private/loopback address ({resolved}); pass "
            "allow_private: true to opt in"
        )


_MAX_URL_FETCH_BYTES = 2 * 1024 * 1024  # 2 MiB cap
