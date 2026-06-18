"""Text extractors and heuristics (API mentions, UI routes, priorities).

Split from analysis.py (issue #292).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


def _extract_api_mentions(spec: str) -> List[Dict[str, Any]]:
    import urllib.parse

    searchable = _without_urls(spec)
    seen: set[tuple[str, str]] = set()
    out: List[Dict[str, Any]] = []

    for match in _API_METHOD_URL.finditer(spec):
        method = match.group(1).upper()
        parsed = urllib.parse.urlparse(match.group(2).rstrip(".,;"))
        path = parsed.path or "/"
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)
        out.append({"method": method, "path": path})

    for match in _API_METHOD_PATH.finditer(searchable):
        method = match.group(1).upper()
        path = match.group(2)
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)
        out.append({"method": method, "path": path})
    if out:
        return out
    for raw in sorted({m.group(0) for m in _API_HINT.finditer(searchable)}):
        if raw.startswith("/"):
            key = ("GET", raw)
            if key not in seen:
                seen.add(key)
                out.append({"method": "GET", "path": raw})
    return out


def _extract_ui_routes(spec: str) -> List[Optional[str]]:
    import urllib.parse

    seen: set[str] = set()
    out: List[Optional[str]] = []

    for match in re.finditer(r"https?://[^\s)`>]+", spec):
        parsed = urllib.parse.urlparse(match.group(0).rstrip(".,;"))
        route = parsed.path or "/"
        if route.lower().startswith(("/api", "/v1", "/v2")):
            continue
        if route not in seen:
            seen.add(route)
            out.append(route)

    for route in _ROUTE_HINT.findall(_without_urls(spec)):
        if route.lower().startswith(("/api", "/v1", "/v2")):
            continue
        if route not in seen:
            seen.add(route)
            out.append(route)

    if "/" in spec and "/" not in seen and re.search(
        r"\b(home|homepage|main page|public blog)\b", spec, re.IGNORECASE
    ):
        out.insert(0, "/")
    return out


def _has_ui_intent(spec: str) -> bool:
    import urllib.parse

    without_headings = re.sub(r"(?m)^#+\s+.*$", " ", spec)
    if _UI_HINT.search(without_headings):
        return True
    for match in re.finditer(r"https?://[^\s)`>]+", spec):
        parsed = urllib.parse.urlparse(match.group(0).rstrip(".,;"))
        path = parsed.path or "/"
        if not path.lower().startswith(("/api", "/v1", "/v2")):
            return True
    return False


def _derive_api_expected_assertion(spec: str, method: str, path: str) -> str:
    method_path = re.escape(method) + r"\s+" + re.escape(path)
    local = spec
    match = re.search(method_path + r"(.{0,180})", spec, re.IGNORECASE | re.DOTALL)
    if match:
        local = match.group(0)
    code = re.search(r"\b(1\d\d|2\d\d|3\d\d|4\d\d|5\d\d)\b", local)
    if code:
        return f"{method} {path} must return HTTP {code.group(1)}"
    if re.search(r"\breject|invalid|negative|error|fail", local, re.IGNORECASE):
        return f"{method} {path} must reject invalid input with an explicit HTTP 4xx status"
    return f"{method} {path} expected behavior must be confirmed by operator before generation"


def _priority_for_text(text: str, *, default: str) -> str:
    if re.search(r"\b(auth|payment|checkout|security|critical|p0)\b", text, re.IGNORECASE):
        return "P1"
    return default


def _first_ui_route(spec: str) -> Optional[str]:
    for route in _ROUTE_HINT.findall(spec):
        if route.lower().startswith(("/api", "/v1", "/v2")):
            continue
        return route
    return None


def _default_cleanup_for_method(method: str, expected: str) -> Optional[str]:
    if method.upper() in {"GET", "HEAD"}:
        return "read-only endpoint"
    if re.search(r"\b4\d\d\b|reject|invalid|negative|error", expected, re.IGNORECASE):
        return "negative path should not create persistent data"
    return None


def _is_negative_or_boundary(text: str) -> bool:
    return bool(re.search(r"\breject|invalid|negative|boundary|error|4\d\d\b", text, re.IGNORECASE))


def _spec_sections(spec: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(spec))
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(spec)
        key = match.group(1).strip().lower()
        body = spec[start:end].strip()
        result[key] = body or "_section empty_"
    return result


def _without_urls(text: str) -> str:
    return re.sub(r"https?://[^\s)`>]+", " ", text)


def _surface_enabled(sut_map: Dict[str, Any], surface: str, *, default: bool) -> bool:
    cfg = sut_map.get("config_snapshot") or {}
    block = cfg.get(surface)
    if isinstance(block, dict) and block.get("enabled") is False:
        return False
    return default


_API_HINT = re.compile(r"(?:GET|POST|PUT|PATCH|DELETE)\s+[/\w{}-]+|/[a-z][\w/{}-]+", re.IGNORECASE)


_API_METHOD_URL = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(https?://[^\s)`>]+)", re.IGNORECASE)


_API_METHOD_PATH = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+([/][\w/{}-]+)", re.IGNORECASE)


_UI_HINT = re.compile(
    r"\b(checkout|login|cart|dashboard|page|homepage|home page|form|button|view|screen|UI|"
    r"website|site|blog|article|navigation|nav|menu|link)\b",
    re.IGNORECASE,
)


_ROUTE_HINT = re.compile(r"(?<!\w)(/[a-z][\w/{}-]+)")


_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
