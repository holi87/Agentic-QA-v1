"""Inbox ingest result/exception data types.

Split from inbox.py (issue #292).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class IngestError(Exception):
    """A single document cannot be parsed into a task payload."""


@dataclass
class IngestResult:
    source: str
    status: str  # "created" | "failed"
    work_item_id: Optional[str] = None
    title: Optional[str] = None
    error: Optional[str] = None
    archived_to: Optional[str] = None


@dataclass
class PdfExtraction:
    """Outcome of probing a PDF for extractable text.

    `status` is one of ``ok`` (above density threshold), ``low`` (likely a
    scan — too few chars per page) or ``failed`` (pypdf missing, parser
    raised, or zero pages). ``message`` is human-readable explanation;
    callers reuse it for IngestError text and dashboard tooltips.
    """

    status: str
    pages: int
    chars: int
    density: float
    text: str = ""
    message: Optional[str] = None
