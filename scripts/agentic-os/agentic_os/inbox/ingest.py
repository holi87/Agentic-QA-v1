"""Inbox directory layout, file listing, and ingest.

Split from inbox.py (issue #292).
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List

from ..errors import InfraError, UsageError
from ..events import EventLog
from ..paths import RuntimePaths
from ..work_items import create_work_item_from_payload

from .files import _move_with_timestamp, _quarantine_failure, _rel
from .parsing import _parse_document
from .types import IngestError, IngestResult


def ingest_inbox(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    *,
    default_sut_root: str = ".",
) -> List[Dict[str, Any]]:
    """Process every pending file in task-intake dirs. Returns per-file results."""
    results: List[IngestResult] = []
    for src in list_inbox_files(paths):
        rel_src = str(src.relative_to(paths.repo_root))
        archive = src.parent / ARCHIVE_DIRNAME
        failed = src.parent / FAILED_DIRNAME
        try:
            payload = _parse_document(src)
        except IngestError as exc:
            dest = _quarantine_failure(src, failed, paths, error=str(exc))
            results.append(IngestResult(
                source=rel_src,
                status="failed",
                error=str(exc),
                archived_to=_rel(paths, dest),
            ))
            continue
        except Exception as exc:  # noqa: BLE001 — never abort the batch
            error = f"unexpected parser error: {exc.__class__.__name__}: {exc}"
            dest = _quarantine_failure(src, failed, paths, error=error)
            results.append(IngestResult(
                source=rel_src,
                status="failed",
                error=error,
                archived_to=_rel(paths, dest),
            ))
            continue
        try:
            detail = create_work_item_from_payload(
                conn, paths, events, payload, default_sut_root=default_sut_root,
            )
        except (UsageError, InfraError) as exc:
            dest = _quarantine_failure(src, failed, paths, error=f"create_work_item: {exc}")
            results.append(IngestResult(
                source=rel_src,
                status="failed",
                error=str(exc),
                archived_to=_rel(paths, dest),
            ))
            continue
        except Exception as exc:  # noqa: BLE001 — never abort the batch
            error = f"create_work_item raised: {exc.__class__.__name__}: {exc}"
            dest = _quarantine_failure(src, failed, paths, error=error)
            results.append(IngestResult(
                source=rel_src,
                status="failed",
                error=error,
                archived_to=_rel(paths, dest),
            ))
            continue
        dest = _move_with_timestamp(src, archive)
        item = detail["work_item"]
        results.append(IngestResult(
            source=rel_src,
            status="created",
            work_item_id=item["id"],
            title=item["title"],
            archived_to=_rel(paths, dest),
        ))
    if results:
        events.write(
            "inbox.ingested",
            actor="operator",
            payload={
                "count": len(results),
                "created": sum(1 for r in results if r.status == "created"),
                "failed": sum(1 for r in results if r.status == "failed"),
                "results": [asdict(r) for r in results],
            },
        )
    return [asdict(r) for r in results]


def list_inbox_files(paths: RuntimePaths) -> List[Path]:
    """Pending files at intake roots (skips .archive / .failed / hidden /
    tracked placeholder files like README.md)."""
    out: List[Path] = []
    for base in intake_dirs(paths):
        if not base.exists():
            continue
        for entry in sorted(base.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.name.lower() in _PLACEHOLDER_FILENAMES:
                continue
            if entry.is_file():
                out.append(entry)
    return sorted(out, key=lambda p: str(p.relative_to(paths.repo_root)))


def intake_dirs(paths: RuntimePaths) -> List[Path]:
    """Return task-intake directories. `inbox/` is canonical; `pretask/`
    is a visible operator alias for dropping larger pre-task bundles."""
    return [paths.repo_root / dirname for dirname in INTAKE_DIRNAMES]


def inbox_dir(paths: RuntimePaths) -> Path:
    return paths.repo_root / INBOX_DIRNAME


INBOX_DIRNAME = "inbox"


PRETASK_DIRNAME = "pretask"


INTAKE_DIRNAMES = (INBOX_DIRNAME, PRETASK_DIRNAME)


ARCHIVE_DIRNAME = ".archive"


FAILED_DIRNAME = ".failed"


_PLACEHOLDER_FILENAMES = {"readme.md"}
