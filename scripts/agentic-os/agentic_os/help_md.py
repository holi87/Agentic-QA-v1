"""Minimal markdown → HTML renderer for the in-product help page.

We deliberately keep zero third-party deps (the rest of the runtime is
stdlib-only). The renderer covers exactly the constructs the dashboard help
markdown actually uses:

- ATX headings (`#`/`##`/`###`) — emit `<h1>`/`<h2>`/`<h3>` with a stable
  slug id so the dashboard can deep-link with `/help#<slug>`.
- Paragraphs separated by blank lines.
- Unordered lists (`- item`) and ordered lists (`1. item`).
- Fenced code blocks (``` ``` ``` ```).
- Inline `code`, **bold**, and `[label](url)` links.
- All other text is HTML-escaped.

Anything we did not list above is rendered as a plain escaped paragraph;
the renderer never raises on unknown syntax. If the markdown ever outgrows
this subset we should switch to a real library, but for the help page a
constrained subset is the safer contract: it forces the doc to stay
predictable for screen readers and we don't have to vendor a parser.
"""
from __future__ import annotations

import html
import re
from typing import List, Tuple

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
_UL_RE = re.compile(r"^[-*]\s+(.+)$")
_OL_RE = re.compile(r"^\d+\.\s+(.+)$")
_FENCE_RE = re.compile(r"^```\s*([A-Za-z0-9_-]*)\s*$")
_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def render(markdown: str) -> str:
    """Render a markdown document to HTML. Returns a `<section>`-free body."""
    lines = markdown.splitlines()
    blocks: List[str] = []
    i = 0
    used_slugs: dict[str, int] = {}
    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip()

        # Fenced code block.
        fence = _FENCE_RE.match(stripped)
        if fence:
            lang = fence.group(1) or ""
            i += 1
            body: List[str] = []
            while i < len(lines) and not _FENCE_RE.match(lines[i].rstrip()):
                body.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1  # consume closing fence
            attr = f' class="lang-{html.escape(lang)}"' if lang else ""
            blocks.append(
                f"<pre><code{attr}>{html.escape(chr(10).join(body))}</code></pre>"
            )
            continue

        # Heading.
        heading = _HEADING_RE.match(stripped)
        if heading:
            level = len(heading.group(1))
            text = heading.group(2)
            slug = _make_slug(text, used_slugs)
            blocks.append(
                f'<h{level} id="{slug}">{_inline(text)}'
                f'<a class="help-anchor" href="#{slug}" aria-label="permalink">¶</a>'
                f"</h{level}>"
            )
            i += 1
            continue

        # Blank line.
        if not stripped:
            i += 1
            continue

        # Unordered list.
        if _UL_RE.match(stripped):
            items, consumed = _gather_list(lines, i, _UL_RE)
            blocks.append("<ul>" + "".join(items) + "</ul>")
            i = consumed
            continue

        # Ordered list.
        if _OL_RE.match(stripped):
            items, consumed = _gather_list(lines, i, _OL_RE)
            blocks.append("<ol>" + "".join(items) + "</ol>")
            i = consumed
            continue

        # Paragraph: collect consecutive non-blank lines that aren't lists /
        # headings / fences.
        para: List[str] = []
        while i < len(lines):
            s = lines[i].rstrip()
            if (
                not s
                or _HEADING_RE.match(s)
                or _UL_RE.match(s)
                or _OL_RE.match(s)
                or _FENCE_RE.match(s)
            ):
                break
            para.append(s)
            i += 1
        if para:
            blocks.append("<p>" + _inline(" ".join(para)) + "</p>")

    return "\n".join(blocks)


_NESTED_UL_RE = re.compile(r"^[-*]\s+(.+)$")
_NESTED_OL_RE = re.compile(r"^\d+\.\s+(.+)$")


def _gather_list(
    lines: List[str], start: int, pattern: re.Pattern[str]
) -> Tuple[List[str], int]:
    """Collect contiguous list items. Indented continuation lines are joined
    onto the previous item; indented bullet lines (with `-`/`*` or `N.`)
    become a single-level nested list under the current item."""
    items: List[str] = []
    current_text: str = ""
    current_nested: List[str] = []
    current_nested_kind: str = ""  # "ul" | "ol" | ""
    i = start

    def flush_item() -> None:
        nonlocal current_text, current_nested, current_nested_kind
        if not current_text and not current_nested:
            return
        nested_html = ""
        if current_nested:
            tag = current_nested_kind or "ul"
            nested_html = f"<{tag}>{''.join(current_nested)}</{tag}>"
        items.append(f"<li>{_inline(current_text)}{nested_html}</li>")
        current_text = ""
        current_nested = []
        current_nested_kind = ""

    while i < len(lines):
        raw = lines[i].rstrip()
        if not raw:
            # Blank line ends the list run (CommonMark's "tight list" model).
            break
        # New top-level item.
        match = pattern.match(raw)
        if match and not lines[i].startswith((" ", "\t")):
            flush_item()
            current_text = match.group(1)
            i += 1
            continue
        # Continuation line — must be indented (or non-matching the marker).
        if lines[i].startswith((" ", "\t")):
            stripped = raw.lstrip()
            nested_ul = _NESTED_UL_RE.match(stripped)
            nested_ol = _NESTED_OL_RE.match(stripped)
            if nested_ul:
                if current_nested_kind and current_nested_kind != "ul":
                    # Mixed nested kinds collapse to ul; rare in practice.
                    pass
                current_nested_kind = current_nested_kind or "ul"
                current_nested.append(f"<li>{_inline(nested_ul.group(1))}</li>")
            elif nested_ol:
                current_nested_kind = current_nested_kind or "ol"
                current_nested.append(f"<li>{_inline(nested_ol.group(1))}</li>")
            else:
                # Plain continuation text — join onto the current item.
                if current_text:
                    current_text = current_text + " " + stripped
                else:
                    current_text = stripped
            i += 1
            continue
        # Unindented, non-matching line ends the list.
        break

    flush_item()
    return items, i


def _inline(text: str) -> str:
    """Run inline replacements while keeping everything else HTML-escaped."""
    out: List[str] = []
    idx = 0

    def push(start: int, end: int) -> None:
        if end > start:
            out.append(html.escape(text[start:end]))

    pattern = re.compile(
        r"`([^`]+)`"                   # inline code
        r"|\*\*([^*]+)\*\*"            # bold
        r"|\[([^\]]+)\]\(([^)]+)\)"    # link
    )
    for match in pattern.finditer(text):
        push(idx, match.start())
        if match.group(1) is not None:
            out.append(f"<code>{html.escape(match.group(1))}</code>")
        elif match.group(2) is not None:
            out.append(f"<strong>{html.escape(match.group(2))}</strong>")
        else:
            label, url = match.group(3), match.group(4)
            out.append(
                f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'
            )
        idx = match.end()
    push(idx, len(text))
    return "".join(out)


def _make_slug(text: str, used: dict[str, int]) -> str:
    base = _SLUG_STRIP.sub("-", text.lower()).strip("-") or "section"
    count = used.get(base, 0)
    used[base] = count + 1
    return base if count == 0 else f"{base}-{count + 1}"
