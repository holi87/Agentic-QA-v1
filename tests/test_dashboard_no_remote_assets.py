"""Regression for issue #200.

The dashboard must render with zero external network dependencies.
This static check asserts that dashboard templates and static CSS/JS
do not reference remote `http://` or `https://` assets.

Allowed exceptions:
  * SVG XML namespaces (`xmlns="http://www.w3.org/2000/svg"`) — pure
    namespace identifiers, not network fetches.
  * URL strings shown as placeholder text inside `placeholder="..."`
    attributes (operator hint, never fetched).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = REPO_ROOT / "scripts" / "agentic-os" / "templates"

# Patterns that are local-runtime safe and may legitimately contain the
# substring "http". Each is matched on a single line and the match is
# removed from that line before scanning for unauthorised URLs.
_ALLOWED_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # SVG namespace declarations.
    re.compile(r"""xmlns(:\w+)?\s*=\s*["']http://www\.w3\.org/[^"']+["']"""),
    # XML namespace strings embedded inside data: URIs.
    re.compile(r"http://www\.w3\.org/\d{4}/svg"),
    # Operator-facing placeholder text — not a fetch.
    re.compile(r"""placeholder\s*=\s*["']https?://[^"']+["']"""),
)

_URL_RE = re.compile(r"https?://")


def _iter_dashboard_files() -> list[Path]:
    files: list[Path] = []
    for path in DASHBOARD_DIR.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in {".html", ".css", ".js"}:
            files.append(path)
    return files


@pytest.mark.parametrize(
    "asset_path",
    _iter_dashboard_files(),
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_dashboard_asset_has_no_remote_urls(asset_path: Path) -> None:
    """Each dashboard template/CSS/JS must not pull remote assets."""
    text = asset_path.read_text(encoding="utf-8")
    offending: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        scrubbed = raw
        for allowed in _ALLOWED_LINE_PATTERNS:
            scrubbed = allowed.sub("", scrubbed)
        if _URL_RE.search(scrubbed):
            offending.append((lineno, raw.strip()))
    assert not offending, (
        f"{asset_path.relative_to(REPO_ROOT)} contains remote http(s):// "
        f"references — dashboard must be offline-safe (#200). "
        f"Offending lines: {offending}"
    )


def test_dashboard_css_has_no_google_fonts_import() -> None:
    """Direct regression for the original audit finding."""
    css = (DASHBOARD_DIR / "static" / "dashboard.css").read_text(encoding="utf-8")
    assert "fonts.googleapis.com" not in css
    assert "fonts.gstatic.com" not in css
    # Defensive: no remote @import at all.
    assert not re.search(r"@import\s+url\(\s*['\"]?https?://", css)
