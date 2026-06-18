"""Same-origin route crawler for exploratory testing of public sites.

This module implements the deterministic core of issue #136 — a
breadth-first crawler that discovers the route surface of a single
origin and surfaces signals (HTTP status, content-type, link counts,
broken assets) that a downstream candidate generator can turn into
test ideas without an LLM prompt.

In-browser console-error / page-error / failed-request capture lives
in :mod:`agentic_os.crawler_browser` (issue #156) — opt-in because it
pulls Chromium. The HTTP core here is dependency-free.

Out of scope here, tracked as follow-up:

- automatic integration into the inbox `public-site` pretask flow
  (issue #157).

The crawler is strictly same-origin (scheme + host + port must match
the start URL); off-origin links are recorded but never fetched.
Loopback / RFC1918 / link-local targets are refused by default so the
crawler cannot be turned into an SSRF probe by a malicious task spec
— operators opt in with ``allow_private=True`` for local fixtures.
``robots.txt`` is honored by default.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Sequence, Set, Tuple

_DEFAULT_USER_AGENT = "agentic-os-crawler/1.0"
_DEFAULT_TIMEOUT_SECONDS = 10
_DEFAULT_MAX_BODY_BYTES = 4 * 1024 * 1024  # 4 MiB cap per page
_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class BrokenAsset:
    url: str
    asset_type: str  # "img" | "script" | "stylesheet" | "other"
    status: Optional[int]
    error: Optional[str]


@dataclass
class ConsoleMessage:
    """Single message emitted to the browser console.

    ``level`` is the Playwright type (``error``/``warning``/``log`` etc.);
    ``text`` is the rendered string. Source URL + line number are
    captured when Playwright provides them, ``None`` otherwise.
    """

    level: str
    text: str
    url: Optional[str] = None
    line: Optional[int] = None


@dataclass
class PageError:
    """Uncaught JS exception or page-level error event."""

    message: str
    stack: Optional[str] = None


@dataclass
class FailedRequest:
    """Network request the browser tried and could not complete."""

    url: str
    method: str
    failure: str
    resource_type: Optional[str] = None


@dataclass
class CrawledRoute:
    url: str
    depth: int
    status: Optional[int]
    content_type: Optional[str]
    elapsed_ms: int
    links_total: int
    links_internal: int
    links_external: int
    error: Optional[str] = None
    broken_assets: List[BrokenAsset] = field(default_factory=list)
    # Issue #156 — populated when the crawl is enriched with Playwright
    # (`--browser` / `enrich_with_browser_signals`). Empty lists mean
    # "no signal observed" when the enrichment ran; whether it ran at
    # all is recorded on the parent ``CrawlReport.browser_enriched``.
    console_errors: List[ConsoleMessage] = field(default_factory=list)
    page_errors: List[PageError] = field(default_factory=list)
    failed_requests: List[FailedRequest] = field(default_factory=list)


@dataclass
class CrawlReport:
    start_url: str
    origin: str
    user_agent: str
    max_pages: int
    max_depth: int
    respect_robots: bool
    allow_private: bool
    pages_visited: int
    pages_skipped_robots: int
    pages_skipped_off_origin: int
    routes: List[CrawledRoute] = field(default_factory=list)
    # Issue #156 — true when ``enrich_with_browser_signals`` ran on this
    # report, so a downstream consumer can tell "no console errors
    # captured" from "browser pass not attempted".
    browser_enriched: bool = False


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------


class _LinkExtractor(HTMLParser):
    """Pull anchor hrefs and asset URLs out of an HTML document.

    The stdlib parser is forgiving with malformed markup, which is what
    we want — a real public site routinely has broken nesting and we
    still want to discover the routes it links to.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: List[str] = []
        self.assets: List[Tuple[str, str]] = []  # (asset_type, url)

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a":
            href = attr_map.get("href", "").strip()
            if href:
                self.links.append(href)
        elif tag == "img":
            src = attr_map.get("src", "").strip()
            if src:
                self.assets.append(("img", src))
        elif tag == "script":
            src = attr_map.get("src", "").strip()
            if src:
                self.assets.append(("script", src))
        elif tag == "link":
            rel = attr_map.get("rel", "").lower()
            href = attr_map.get("href", "").strip()
            if href and "stylesheet" in rel.split():
                self.assets.append(("stylesheet", href))


def _parse_html(body: str) -> Tuple[List[str], List[Tuple[str, str]]]:
    parser = _LinkExtractor()
    try:
        parser.feed(body)
    except Exception:
        # Malformed input must not abort the whole crawl — the parser
        # already collected whatever it managed to read.
        pass
    return parser.links, parser.assets


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------


def _origin(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError(f"invalid URL (missing scheme/host): {url!r}")
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{parsed.hostname}:{port}"


def _is_same_origin(candidate: str, origin: str) -> bool:
    try:
        return _origin(candidate) == origin
    except ValueError:
        return False


def _check_private(url: str, *, allow_private: bool) -> Optional[str]:
    """Return an error string when the URL must be refused, else None.

    Mirrors ``analysis._validate_url_host_not_private`` but returns the
    reason instead of raising so the crawler can keep going on
    subsequent links.
    """
    if allow_private:
        return None
    parsed = urllib.parse.urlparse(url)
    if not parsed.hostname:
        return "missing host"
    try:
        resolved = socket.gethostbyname(parsed.hostname)
    except OSError as exc:
        return f"DNS resolution failed: {exc}"
    addr = ipaddress.ip_address(resolved)
    if addr.is_loopback or addr.is_private or addr.is_link_local:
        return f"host {parsed.hostname} → {resolved} is loopback/private/link-local"
    return None


def _fetch_html(
    url: str,
    *,
    user_agent: str,
    timeout: int,
    allow_private: bool,
) -> Tuple[Optional[int], Optional[str], Optional[str], Optional[str]]:
    """Return ``(status, content_type, body_or_None, error_or_None)``.

    Errors are returned, not raised, so a single broken page doesn't
    sink the whole BFS.
    """
    refusal = _check_private(url, allow_private=allow_private)
    if refusal is not None:
        return None, None, None, refusal
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            ctype = (resp.headers.get("content-type") or "").lower()
            body_bytes = resp.read(_DEFAULT_MAX_BODY_BYTES + 1)
            if len(body_bytes) > _DEFAULT_MAX_BODY_BYTES:
                return status, ctype, None, (
                    f"body exceeds {_DEFAULT_MAX_BODY_BYTES} byte cap"
                )
            if not any(t in ctype for t in _HTML_CONTENT_TYPES):
                # Still record status — caller decides whether to parse.
                return status, ctype, None, None
            encoding = resp.headers.get_content_charset() or "utf-8"
            try:
                body = body_bytes.decode(encoding, errors="replace")
            except LookupError:
                body = body_bytes.decode("utf-8", errors="replace")
            return status, ctype, body, None
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("content-type"), None, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, None, None, f"URL error: {exc.reason}"
    except (TimeoutError, socket.timeout) as exc:
        return None, None, None, f"timeout: {exc}"


def _head_check(
    url: str,
    *,
    user_agent: str,
    timeout: int,
    allow_private: bool,
) -> Tuple[Optional[int], Optional[str]]:
    """HEAD an asset URL; fall back to range-bounded GET when HEAD fails."""
    refusal = _check_private(url, allow_private=allow_private)
    if refusal is not None:
        return None, refusal
    headers = {"User-Agent": user_agent}
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if method == "GET":
                    # Drain a single chunk so connection pools recycle cleanly.
                    resp.read(1024)
                return resp.status, None
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code in {405, 501}:
                continue
            return exc.code, f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            return None, f"URL error: {exc.reason}"
        except (TimeoutError, socket.timeout) as exc:
            return None, f"timeout: {exc}"
    return None, "asset unreachable"


def _load_robots(
    origin: str,
    *,
    user_agent: str,
    timeout: int,
    allow_private: bool,
) -> Optional[urllib.robotparser.RobotFileParser]:
    """Return a configured ``RobotFileParser`` or ``None`` on fetch failure.

    A missing ``robots.txt`` returns an empty parser that allows all
    paths, matching the de-facto standard.
    """
    robots_url = origin.rstrip("/") + "/robots.txt"
    if _check_private(robots_url, allow_private=allow_private) is not None:
        return None
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    req = urllib.request.Request(robots_url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(_DEFAULT_MAX_BODY_BYTES).decode(
                resp.headers.get_content_charset() or "utf-8", errors="replace"
            )
        rp.parse(body.splitlines())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            rp.parse([])  # empty → allow all
        else:
            return None
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        return None
    return rp


# ---------------------------------------------------------------------------
# Crawl driver
# ---------------------------------------------------------------------------


def crawl_same_origin(
    start_url: str,
    *,
    max_depth: int = 2,
    max_pages: int = 25,
    user_agent: str = _DEFAULT_USER_AGENT,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    respect_robots: bool = True,
    allow_private: bool = False,
) -> CrawlReport:
    """Breadth-first crawl restricted to ``start_url``'s origin.

    Parameters mirror the CLI flags. Returns a ``CrawlReport`` that the
    caller can serialise with ``crawl_report_to_json``.
    """
    if max_depth < 0:
        raise ValueError("max_depth must be >= 0")
    if max_pages <= 0:
        raise ValueError("max_pages must be >= 1")

    origin = _origin(start_url)
    refusal = _check_private(start_url, allow_private=allow_private)
    if refusal is not None:
        raise ValueError(f"refusing to crawl start URL: {refusal}")

    robots: Optional[urllib.robotparser.RobotFileParser] = None
    if respect_robots:
        robots = _load_robots(
            origin,
            user_agent=user_agent,
            timeout=timeout_seconds,
            allow_private=allow_private,
        )

    report = CrawlReport(
        start_url=start_url,
        origin=origin,
        user_agent=user_agent,
        max_pages=max_pages,
        max_depth=max_depth,
        respect_robots=respect_robots,
        allow_private=allow_private,
        pages_visited=0,
        pages_skipped_robots=0,
        pages_skipped_off_origin=0,
    )

    queue: List[Tuple[str, int]] = [(start_url, 0)]
    visited: Set[str] = set()
    asset_cache: Dict[str, Tuple[Optional[int], Optional[str]]] = {}

    while queue and report.pages_visited < max_pages:
        url, depth = queue.pop(0)
        norm = _normalize_url(url)
        if norm in visited:
            continue
        visited.add(norm)

        if robots is not None and not robots.can_fetch(user_agent, url):
            report.pages_skipped_robots += 1
            continue

        started = time.monotonic()
        status, ctype, body, error = _fetch_html(
            url,
            user_agent=user_agent,
            timeout=timeout_seconds,
            allow_private=allow_private,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)

        links_internal = 0
        links_external = 0
        broken: List[BrokenAsset] = []

        if body:
            links, assets = _parse_html(body)
            for href in links:
                resolved = urllib.parse.urljoin(url, href)
                resolved = _strip_fragment(resolved)
                if not resolved:
                    continue
                if _is_same_origin(resolved, origin):
                    links_internal += 1
                    if depth + 1 <= max_depth and _normalize_url(resolved) not in visited:
                        queue.append((resolved, depth + 1))
                else:
                    links_external += 1
            for asset_type, src in assets:
                resolved = urllib.parse.urljoin(url, src)
                resolved = _strip_fragment(resolved)
                if not resolved:
                    continue
                if resolved in asset_cache:
                    a_status, a_error = asset_cache[resolved]
                else:
                    a_status, a_error = _head_check(
                        resolved,
                        user_agent=user_agent,
                        timeout=timeout_seconds,
                        allow_private=allow_private,
                    )
                    asset_cache[resolved] = (a_status, a_error)
                if _is_asset_broken(a_status, a_error):
                    broken.append(
                        BrokenAsset(
                            url=resolved,
                            asset_type=asset_type,
                            status=a_status,
                            error=a_error,
                        )
                    )

        report.routes.append(
            CrawledRoute(
                url=url,
                depth=depth,
                status=status,
                content_type=ctype,
                elapsed_ms=elapsed_ms,
                links_total=links_internal + links_external,
                links_internal=links_internal,
                links_external=links_external,
                error=error,
                broken_assets=broken,
            )
        )
        report.pages_visited += 1

    return report


def _normalize_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    # Drop trailing fragments and lowercase host so the visited set
    # collapses URL variants that point at the same resource.
    return urllib.parse.urlunparse(
        (
            parsed.scheme.lower(),
            (parsed.netloc or "").lower(),
            parsed.path or "/",
            parsed.params,
            parsed.query,
            "",
        )
    )


def _strip_fragment(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    return urllib.parse.urlunparse(parsed._replace(fragment=""))


def _is_asset_broken(status: Optional[int], error: Optional[str]) -> bool:
    if error is not None and status is None:
        return True
    if status is None:
        return True
    return status >= 400


def crawl_report_to_json(report: CrawlReport) -> Dict:
    """Serialise a ``CrawlReport`` to the wire-format the CLI emits."""
    return {
        "start_url": report.start_url,
        "origin": report.origin,
        "user_agent": report.user_agent,
        "max_depth": report.max_depth,
        "max_pages": report.max_pages,
        "respect_robots": report.respect_robots,
        "allow_private": report.allow_private,
        "browser_enriched": report.browser_enriched,
        "summary": {
            "pages_visited": report.pages_visited,
            "pages_skipped_robots": report.pages_skipped_robots,
            "pages_skipped_off_origin": report.pages_skipped_off_origin,
            "total_routes": len(report.routes),
            "broken_assets_total": sum(len(r.broken_assets) for r in report.routes),
            "console_errors_total": sum(len(r.console_errors) for r in report.routes),
            "page_errors_total": sum(len(r.page_errors) for r in report.routes),
            "failed_requests_total": sum(len(r.failed_requests) for r in report.routes),
        },
        "routes": [
            {
                "url": r.url,
                "depth": r.depth,
                "status": r.status,
                "content_type": r.content_type,
                "elapsed_ms": r.elapsed_ms,
                "links_total": r.links_total,
                "links_internal": r.links_internal,
                "links_external": r.links_external,
                "error": r.error,
                "broken_assets": [asdict(b) for b in r.broken_assets],
                "console_errors": [asdict(c) for c in r.console_errors],
                "page_errors": [asdict(p) for p in r.page_errors],
                "failed_requests": [asdict(f) for f in r.failed_requests],
            }
            for r in report.routes
        ],
    }


def crawl_report_to_str(report: CrawlReport, *, indent: int = 2) -> str:
    return json.dumps(crawl_report_to_json(report), indent=indent, sort_keys=False)
