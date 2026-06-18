"""local docs ingest.

Reads .md / .txt / .html docs from the operator's repo, computes hash and
timestamp, and extracts heading-delimited requirement sections so the planner
can cite them.

URL fetch for OpenAPI / docs sources lives in `analysis._safe_fetch_url`
(issue #78). That path enforces: 10 s timeout, 2 MiB cap, content-type
allowlist, refusal of loopback/RFC1918/link-local targets, and redirect
re-validation against the private-network policy. This module stays
file-only on purpose so that disk ingest can never be tricked into a
network call.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from .errors import UsageError
from .time_utils import now_iso


MAX_DOC_BYTES = 256 * 1024  # 256 KiB ceiling — operator docs, not full books
_SECTION_HEADER_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class DocSection:
    heading: str
    body: str


@dataclass(frozen=True)
class IngestedDoc:
    source_path: str
    source_hash: str
    ingested_at: str
    size_bytes: int
    media_type: str
    sections: List[DocSection]
    text: str


def ingest_local_doc(path: Path) -> IngestedDoc:
    """Read a single .md/.txt/.html file from disk."""
    if not path.exists() or not path.is_file():
        raise UsageError(f"docs file not found: {path}")
    suffix = path.suffix.lower()
    if suffix not in {".md", ".txt", ".html"}:
        raise UsageError(f"unsupported docs extension: {suffix}")
    raw = path.read_bytes()
    if len(raw) > MAX_DOC_BYTES:
        raise UsageError(
            f"docs file exceeds {MAX_DOC_BYTES} byte ceiling: {path} ({len(raw)} bytes)"
        )
    text = raw.decode("utf-8", errors="replace")
    sections = _split_sections(text)
    media_type = {
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".html": "text/html",
    }[suffix]
    return IngestedDoc(
        source_path=str(path),
        source_hash=hashlib.sha256(raw).hexdigest(),
        ingested_at=now_iso(),
        size_bytes=len(raw),
        media_type=media_type,
        sections=sections,
        text=text,
    )


def _split_sections(text: str) -> List[DocSection]:
    """Split Markdown-like text on H1–H6 headings."""
    matches = list(_SECTION_HEADER_RE.finditer(text))
    if not matches:
        return [DocSection(heading="(document)", body=text.strip())]
    sections: List[DocSection] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append(
            DocSection(heading=m.group(1).strip(), body=text[start:end].strip())
        )
    return sections


def ingested_to_dict(doc: IngestedDoc) -> Dict[str, object]:
    return {
        "source_path": doc.source_path,
        "source_hash": doc.source_hash,
        "ingested_at": doc.ingested_at,
        "size_bytes": doc.size_bytes,
        "media_type": doc.media_type,
        "sections": [
            {"heading": s.heading, "body_len": len(s.body)} for s in doc.sections
        ],
    }
