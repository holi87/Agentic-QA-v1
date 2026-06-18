"""Inbox intake — package shim (split from inbox.py per issue #292).

Public surface re-exported from submodules so existing
`from agentic_os.inbox import …` imports keep working.
"""
from __future__ import annotations

from .types import (  # noqa: F401
    IngestError,
    IngestResult,
    PdfExtraction,
)
from .files import (  # noqa: F401
    _DEFAULT_PRIORITY,
    _clean_markdown_line,
    _clip,
    _dedupe,
    _first_h1,
    _first_nonempty,
    _highest_priority,
    _is_metadata_line,
    _markdown_list_or_default,
    _move_with_timestamp,
    _quarantine_failure,
    _rel,
)
from .crawl import (  # noqa: F401
    INTAKE_TYPE_PUBLIC_SITE,
    _PUBLIC_SITE_CRAWL_DEPTH,
    _PUBLIC_SITE_CRAWL_MAX_PAGES,
    _crawl_public_sites,
    _persist_crawl_reports,
)
from .parsing import (  # noqa: F401
    EXTRACTION_STATUS_FAILED,
    EXTRACTION_STATUS_LOW,
    EXTRACTION_STATUS_OK,
    EXTRACTION_STATUS_UNSUPPORTED,
    SUPPORTED_EXTS,
    _DEFAULT_SUT_ROOT,
    _MAX_INPUT_BYTES,
    _MIN_PDF_CHARS_PER_PAGE,
    _PRIORITY_RE,
    _SCANNED_PDF_HINT,
    _START_URL_RE,
    _SUT_ROOT_RE,
    _TYPE_RE,
    _VALID_PRIORITIES,
    _extract_docx,
    _extract_intake_type,
    _extract_pdf,
    _extract_priority,
    _extract_start_url,
    _extract_sut_root,
    _parse_document,
    _payload_from_markdown,
    _payload_from_plaintext,
    _read_utf8,
    _safe_call,
    classify_intake_file,
    inspect_pdf,
)
from .ingest import (  # noqa: F401
    ARCHIVE_DIRNAME,
    FAILED_DIRNAME,
    INBOX_DIRNAME,
    INTAKE_DIRNAMES,
    PRETASK_DIRNAME,
    _PLACEHOLDER_FILENAMES,
    inbox_dir,
    ingest_inbox,
    intake_dirs,
    list_inbox_files,
)
from .synthesis import (  # noqa: F401
    _build_synthesis_payload,
    _extract_constraint_lines,
    _extract_known_bug_lines,
    _extract_requirement_lines,
    _extract_surfaces,
    _render_synthesized_markdown,
    _synthesis_title,
    synthesize_inbox_task,
)
