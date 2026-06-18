"""Document parsing, PDF/DOCX extraction, intake classification, field extractors.

Split from inbox.py (issue #292).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import agentic_os.inbox as _ibx  # _extract_pdf monkey-patch surface (tests patch the package)

from .files import _DEFAULT_PRIORITY, _first_h1, _first_nonempty
from .types import IngestError, PdfExtraction


def _parse_document(path: Path) -> Dict[str, Any]:
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise IngestError(
            f"unsupported extension {ext!r}; supported: {sorted(SUPPORTED_EXTS)}"
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise IngestError(f"cannot stat file: {exc}") from exc
    if size == 0:
        raise IngestError("document is empty")
    if size > _MAX_INPUT_BYTES:
        raise IngestError(
            f"file exceeds {_MAX_INPUT_BYTES} byte limit ({size} bytes)"
        )
    if ext in (".md", ".markdown"):
        text = _read_utf8(path)
        return _payload_from_markdown(text, fallback_title=path.stem)
    if ext == ".txt":
        text = _read_utf8(path)
        return _payload_from_plaintext(text, fallback_title=path.stem)
    if ext == ".docx":
        text = _safe_call(_extract_docx, path, kind="docx")
        return _payload_from_plaintext(text, fallback_title=path.stem)
    if ext == ".pdf":
        text = _safe_call(_ibx._extract_pdf, path, kind="pdf")
        return _payload_from_plaintext(text, fallback_title=path.stem)
    raise IngestError(f"no parser registered for {ext}")  # defensive


def _payload_from_markdown(text: str, *, fallback_title: str) -> Dict[str, Any]:
    text = text.strip()
    if not text:
        raise IngestError("document is empty")
    h1 = _first_h1(text)
    title = (h1 or _first_nonempty(text) or fallback_title)[:160]
    if h1:
        # Caller already supplied a full markdown spec — honor any inline
        # `Priority:` / `SUT root:` / `Type:` / `Start URL:` metadata the
        # document carries so the DB row stays consistent with the persisted
        # spec file.
        priority = _extract_priority(text)
        sut_root = _extract_sut_root(text)
        intake_type = _extract_intake_type(text)
        start_url = _extract_start_url(text)
        payload: Dict[str, Any] = {
            "title": title,
            "priority": priority,
            "spec_markdown": text,
        }
        if sut_root is not None:
            payload["sut_root"] = sut_root
        if intake_type is not None:
            payload["intake_type"] = intake_type
        if start_url is not None:
            payload["start_url"] = start_url
        return payload
    # Plain markdown without H1 — wrap so downstream parsers find a title.
    wrapped = (
        f"# {title}\n\n"
        f"Priority: {_DEFAULT_PRIORITY}\n"
        f"SUT root: {_DEFAULT_SUT_ROOT}\n\n"
        f"## Expected behavior\n{text}\n"
    )
    return {"title": title, "priority": _DEFAULT_PRIORITY, "spec_markdown": wrapped}


def _payload_from_plaintext(text: str, *, fallback_title: str) -> Dict[str, Any]:
    text = text.strip()
    if not text:
        raise IngestError("document is empty")
    title = (_first_nonempty(text) or fallback_title)[:160]
    body = text
    markdown = (
        f"# {title}\n\n"
        f"Priority: {_DEFAULT_PRIORITY}\n"
        f"SUT root: {_DEFAULT_SUT_ROOT}\n\n"
        f"## Expected behavior\n{body}\n"
    )
    return {"title": title, "priority": _DEFAULT_PRIORITY, "spec_markdown": markdown}


def inspect_pdf(path: Path) -> PdfExtraction:
    """Probe `path` for extractable text and classify the result.

    Never raises — failures are reported via the returned status so the
    dashboard can render a status badge without crashing the list endpoint.
    Callers that need the text (ingest path) wrap this in `_extract_pdf`
    which converts non-`ok` outcomes into `IngestError`.
    """
    try:
        import pypdf  # type: ignore[import-untyped]
    except ImportError:
        return PdfExtraction(
            status=EXTRACTION_STATUS_FAILED,
            pages=0,
            chars=0,
            density=0.0,
            message=(
                "pdf parsing requires pypdf; "
                "install pypdf to enable .pdf ingest"
            ),
        )
    try:
        reader = pypdf.PdfReader(str(path))
        pages = list(reader.pages)
    except Exception as exc:  # noqa: BLE001 — defensive boundary for any pypdf failure
        return PdfExtraction(
            status=EXTRACTION_STATUS_FAILED,
            pages=0,
            chars=0,
            density=0.0,
            message=f"pdf parser failed: {exc.__class__.__name__}: {exc}",
        )
    if not pages:
        return PdfExtraction(
            status=EXTRACTION_STATUS_FAILED,
            pages=0,
            chars=0,
            density=0.0,
            message="pdf contained zero pages",
        )
    parts: List[str] = []
    chars = 0
    for page in pages:
        try:
            txt = (page.extract_text() or "").strip()
        except Exception as exc:  # noqa: BLE001 — single page failing must not abort probe
            return PdfExtraction(
                status=EXTRACTION_STATUS_FAILED,
                pages=len(pages),
                chars=0,
                density=0.0,
                message=f"pdf parser failed on a page: {exc.__class__.__name__}: {exc}",
            )
        if txt:
            parts.append(txt)
            chars += len(txt)
    page_count = len(pages)
    density = chars / page_count if page_count else 0.0
    if chars == 0:
        return PdfExtraction(
            status=EXTRACTION_STATUS_LOW,
            pages=page_count,
            chars=0,
            density=0.0,
            message=(
                f"pdf contained no extractable text across {page_count} page(s). "
                f"{_SCANNED_PDF_HINT}"
            ),
        )
    if density < _MIN_PDF_CHARS_PER_PAGE:
        return PdfExtraction(
            status=EXTRACTION_STATUS_LOW,
            pages=page_count,
            chars=chars,
            density=density,
            text="\n\n".join(parts),
            message=(
                f"pdf text density {density:.1f} chars/page is below threshold "
                f"{_MIN_PDF_CHARS_PER_PAGE}. {_SCANNED_PDF_HINT}"
            ),
        )
    return PdfExtraction(
        status=EXTRACTION_STATUS_OK,
        pages=page_count,
        chars=chars,
        density=density,
        text="\n\n".join(parts),
    )


def _extract_pdf(path: Path) -> str:
    result = inspect_pdf(path)
    if result.status == EXTRACTION_STATUS_OK:
        return result.text
    message = result.message or f"pdf extraction status: {result.status}"
    raise IngestError(message)


def _extract_docx(path: Path) -> str:
    try:
        from docx import Document  # type: ignore[import-untyped]
    except ImportError as exc:
        raise IngestError(
            "docx parsing requires python-docx; "
            "install python-docx to enable .docx ingest"
        ) from exc
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    if not parts:
        raise IngestError("docx contained no readable paragraphs")
    return "\n\n".join(parts)


def classify_intake_file(path: Path) -> Dict[str, Any]:
    """Return per-file extraction status for the dashboard inbox list.

    Non-PDF inputs are reported as ``ok`` without I/O — text-based formats
    cannot be "scanned" in a way that defeats extraction. PDFs are probed
    eagerly because the polling cost is bounded by `_MAX_INPUT_BYTES`.
    """
    ext = path.suffix.lower()
    if ext == ".pdf":
        result = inspect_pdf(path)
        info: Dict[str, Any] = {
            "status": result.status,
            "pages": result.pages,
            "chars": result.chars,
            "density": round(result.density, 1),
        }
        if result.message:
            info["message"] = result.message
        return info
    if ext in SUPPORTED_EXTS:
        return {"status": EXTRACTION_STATUS_OK}
    return {
        "status": EXTRACTION_STATUS_UNSUPPORTED,
        "message": f"unsupported extension {ext!r}",
    }


def _safe_call(fn, path: Path, *, kind: str) -> str:
    """Wrap third-party parser calls so a malformed binary aborts a single
    file instead of the whole ingest batch. IngestError carries the intent;
    arbitrary library exceptions get re-raised as IngestError with the
    library's own message."""
    try:
        return fn(path)
    except IngestError:
        raise
    except Exception as exc:  # noqa: BLE001 — defensive boundary
        raise IngestError(f"{kind} parser failed: {exc.__class__.__name__}: {exc}") from exc


def _read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise IngestError("file must be UTF-8") from exc


SUPPORTED_EXTS = {".md", ".markdown", ".txt", ".docx", ".pdf"}


_MAX_INPUT_BYTES = 4 * 1024 * 1024  # 4 MiB cap; documents larger than this


_MIN_PDF_CHARS_PER_PAGE = 50


EXTRACTION_STATUS_OK = "ok"


EXTRACTION_STATUS_LOW = "low"


EXTRACTION_STATUS_FAILED = "failed"


EXTRACTION_STATUS_UNSUPPORTED = "unsupported"


_SCANNED_PDF_HINT = "Scanned PDFs are not supported — provide an extractable-text PDF."


_DEFAULT_SUT_ROOT = "."


def _extract_priority(text: str) -> str:
    match = _PRIORITY_RE.search(text)
    if not match:
        return _DEFAULT_PRIORITY
    value = match.group(1).strip().upper()
    if value not in _VALID_PRIORITIES:
        # work_items.create_work_item_from_payload would reject this anyway;
        # fall back to the default so the document still ingests.
        return _DEFAULT_PRIORITY
    return value


def _extract_sut_root(text: str) -> Optional[str]:
    match = _SUT_ROOT_RE.search(text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _extract_intake_type(text: str) -> Optional[str]:
    """Pull the intake `Type:` field (e.g. ``public-site``) from a doc."""
    match = _TYPE_RE.search(text)
    if not match:
        return None
    return match.group(1).strip().lower() or None


def _extract_start_url(text: str) -> Optional[str]:
    """Pull the intake `Start URL:` field. Returned verbatim; the crawler
    validates URL shape and SSRF guards reject unsafe targets."""
    match = _START_URL_RE.search(text)
    if not match:
        return None
    return match.group(1).strip() or None


_VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}


_PRIORITY_RE = re.compile(r"^\s*Priority\s*:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)


_SUT_ROOT_RE = re.compile(r"^\s*SUT\s+root\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


_TYPE_RE = re.compile(r"^\s*Type\s*:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)


_START_URL_RE = re.compile(r"^\s*Start\s+URL\s*:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)
