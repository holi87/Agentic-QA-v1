"""Synthesize an inbox task from parsed intake documents.

Split from inbox.py (issue #292).
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..errors import InfraError, UsageError
from ..events import EventLog
from ..paths import RuntimePaths
from ..work_items import create_work_item_from_payload

from .crawl import _crawl_public_sites, _persist_crawl_reports
from .files import _DEFAULT_PRIORITY, _clean_markdown_line, _clip, _dedupe, _first_nonempty, _highest_priority, _is_metadata_line, _markdown_list_or_default, _move_with_timestamp, _quarantine_failure, _rel
from .ingest import ARCHIVE_DIRNAME, FAILED_DIRNAME, list_inbox_files
from .parsing import _DEFAULT_SUT_ROOT, _parse_document
from .types import IngestError, IngestResult


def synthesize_inbox_task(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    *,
    title: Optional[str] = None,
    default_sut_root: str = ".",
    allow_private_crawl: bool = False,
) -> Dict[str, Any]:
    """Create one task spec from all pending intake documents.

    `ingest_inbox()` is intentionally one-file-one-task. This function is the
    operator-friendly pre-task path: drop product docs, feature notes, QA
    constraints or bug notes into `inbox/` or `pretask/`, then create one
    structured task brief that preserves source references and extracts the
    most useful surfaces and requirements for the planner.

    Issue #157 — any intake doc carrying ``Type: public-site`` + ``Start URL:``
    metadata triggers a deterministic same-origin HTTP crawl of that URL
    during synthesis. Discovered routes are appended to the rendered spec's
    "Relevant endpoints or pages" section and the full crawl report is
    persisted under ``agentic-os-runtime/inbox/crawls/<work_item_id>.json``
    so analyze/plan stages can read structured data without parsing markdown.

    ``allow_private_crawl`` mirrors the crawler's flag — default refuses
    loopback/RFC1918 targets so a poisoned intake doc cannot turn synthesis
    into an SSRF probe. Tests against tmp HTTP servers opt in explicitly.
    """
    pending = list_inbox_files(paths)
    if not pending:
        return {
            "status": "empty",
            "created": 0,
            "failed": 0,
            "source_count": 0,
            "results": [],
        }

    parsed: List[Dict[str, Any]] = []
    results: List[IngestResult] = []
    for src in pending:
        rel_src = str(src.relative_to(paths.repo_root))
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
        parsed.append({"source": rel_src, "path": src, "payload": payload})

    if not parsed:
        payload = {
            "status": "failed",
            "created": 0,
            "failed": len(results),
            "source_count": 0,
            "results": [asdict(r) for r in results],
        }
        events.write("inbox.synthesized", actor="operator", payload=payload)
        return payload

    crawl_reports, crawl_summaries = _crawl_public_sites(
        parsed, allow_private=allow_private_crawl
    )
    task_title = _synthesis_title(parsed, title=title)
    task_payload = _build_synthesis_payload(
        parsed,
        title=task_title,
        default_sut_root=default_sut_root,
        crawl_reports=crawl_reports,
    )
    try:
        detail = create_work_item_from_payload(
            conn, paths, events, task_payload, default_sut_root=default_sut_root,
        )
    except (UsageError, InfraError) as exc:
        error = f"create_work_item: {exc}"
        result_error = str(exc)
    except Exception as exc:  # noqa: BLE001 — defensive boundary for batch ingest
        error = f"create_work_item raised: {exc.__class__.__name__}: {exc}"
        result_error = error
    else:
        error = ""
        result_error = ""

    if error:
        for item in parsed:
            src = item["path"]
            dest = _quarantine_failure(src, src.parent / FAILED_DIRNAME, paths, error=error)
            results.append(IngestResult(
                source=item["source"],
                status="failed",
                error=result_error,
                archived_to=_rel(paths, dest),
            ))
        payload = {
            "status": "failed",
            "created": 0,
            "failed": len(results),
            "source_count": len(parsed),
            "results": [asdict(r) for r in results],
        }
        events.write("inbox.synthesized", actor="operator", payload=payload)
        return payload

    item = detail["work_item"]
    for source in parsed:
        src = source["path"]
        dest = _move_with_timestamp(src, src.parent / ARCHIVE_DIRNAME)
        results.append(IngestResult(
            source=source["source"],
            status="created",
            work_item_id=item["id"],
            title=item["title"],
            archived_to=_rel(paths, dest),
        ))

    crawl_report_paths = _persist_crawl_reports(
        paths, work_item_id=item["id"], crawl_reports=crawl_reports
    )

    payload = {
        "status": "created",
        "created": 1,
        "failed": sum(1 for r in results if r.status == "failed"),
        "source_count": len(parsed),
        "work_item_id": item["id"],
        "title": item["title"],
        "spec_path": item["spec_path"],
        "results": [asdict(r) for r in results],
    }
    if crawl_summaries:
        payload["crawled_sites"] = crawl_summaries
        payload["crawl_reports"] = crawl_report_paths
    events.write("inbox.synthesized", actor="operator", payload=payload)
    return payload


def _build_synthesis_payload(
    parsed: List[Dict[str, Any]],
    *,
    title: str,
    default_sut_root: str,
    crawl_reports: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    priority = _highest_priority(
        str(item["payload"].get("priority") or _DEFAULT_PRIORITY) for item in parsed
    )
    sut_roots = sorted({
        str(item["payload"].get("sut_root") or default_sut_root or _DEFAULT_SUT_ROOT).strip()
        for item in parsed
        if str(item["payload"].get("sut_root") or default_sut_root or _DEFAULT_SUT_ROOT).strip()
    })
    sut_root = sut_roots[0] if len(sut_roots) == 1 else (default_sut_root or _DEFAULT_SUT_ROOT)
    markdown = _render_synthesized_markdown(
        parsed,
        title=title,
        priority=priority,
        sut_root=sut_root,
        multiple_sut_roots=len(sut_roots) > 1,
        sut_roots=sut_roots,
        crawl_reports=crawl_reports or [],
    )
    return {
        "title": title,
        "priority": priority,
        "sut_root": sut_root,
        "spec_markdown": markdown,
    }


def _render_synthesized_markdown(
    parsed: List[Dict[str, Any]],
    *,
    title: str,
    priority: str,
    sut_root: str,
    multiple_sut_roots: bool,
    sut_roots: List[str],
    crawl_reports: Optional[List[Dict[str, Any]]] = None,
) -> str:
    surfaces: List[str] = []
    constraints: List[str] = []
    requirement_blocks: List[str] = []
    source_lines: List[str] = []

    for idx, item in enumerate(parsed, start=1):
        payload = item["payload"]
        source = str(item["source"])
        doc_title = str(payload.get("title") or Path(source).stem)
        text = str(payload.get("spec_markdown") or "")
        source_lines.append(f"- [{idx}] `{source}` - {doc_title}")
        doc_requirements = _extract_requirement_lines(text, limit=6)
        doc_surfaces = _extract_surfaces(text, limit=8)
        doc_constraints = _extract_constraint_lines(text, limit=4)
        surfaces.extend(doc_surfaces)
        constraints.extend(doc_constraints)
        if doc_requirements:
            requirement_blocks.append(
                f"### [{idx}] {doc_title}\n"
                + "\n".join(f"- {line}" for line in doc_requirements)
            )
        else:
            requirement_blocks.append(
                f"### [{idx}] {doc_title}\n"
                f"- {_clip(_first_nonempty(text) or 'No concise requirement line detected.', 220)}"
            )

    # Issue #157 — surface crawler discoveries alongside doc-extracted
    # endpoints so the planner sees the same union the operator does.
    crawled_routes: List[str] = []
    crawled_problems: List[str] = []
    for report in crawl_reports or []:
        origin = report.get("origin") or report.get("start_url") or ""
        routes_seen = 0
        for route in report.get("routes") or []:
            url = route.get("url")
            if not url:
                continue
            status = route.get("status")
            ctype = route.get("content_type") or ""
            label = f"GET {url}"
            if status is not None:
                label += f" → HTTP {status}"
            if ctype:
                label += f" ({ctype.split(';')[0].strip()})"
            crawled_routes.append(label)
            routes_seen += 1
            if status is not None and status >= 400:
                crawled_problems.append(
                    f"crawler: {url} returned HTTP {status} during pre-task discovery"
                )
            elif route.get("error"):
                crawled_problems.append(
                    f"crawler: {url} unreachable ({route['error']})"
                )
            for asset in route.get("broken_assets") or []:
                a_url = asset.get("url")
                a_status = asset.get("status")
                a_err = asset.get("error")
                tail = f"HTTP {a_status}" if a_status is not None else (a_err or "unreachable")
                crawled_problems.append(
                    f"crawler: broken {asset.get('asset_type') or 'asset'} {a_url} → {tail}"
                )
        if not routes_seen and origin:
            crawled_problems.append(
                f"crawler: no routes returned from {origin} — verify the start URL is reachable"
            )
    surfaces.extend(crawled_routes)
    surfaces = _dedupe(surfaces)[:40]
    constraints = _dedupe(constraints)[:12]
    open_questions = [
        "Confirm which synthesized candidates should become executable tests before approving generation.",
    ]
    if not surfaces:
        open_questions.append("No explicit endpoints, URLs or page paths were detected in the intake documents.")
    if not constraints:
        open_questions.append("No explicit test data or credential constraints were detected.")
    if multiple_sut_roots:
        open_questions.append(
            "Multiple SUT roots appeared in source docs: " + ", ".join(f"`{root}`" for root in sut_roots)
        )

    lines = [
        f"# {title}",
        "",
        f"Priority: {priority}",
        f"SUT root: {sut_root}",
        "",
        "## Business goal",
        "Create actionable QA coverage from the uploaded documentation bundle. "
        "Prioritize business-visible assertions, critical user paths, exact documented API/page behavior, "
        "known bugs, and evidence that can be verified by Agentic OS.",
        "",
        "## Source documents",
        *source_lines,
        "",
        "## Expected behavior",
        "\n\n".join(requirement_blocks),
        "",
        "## In scope",
        "- Analyze the source documents as one feature/testing brief.",
        "- Generate candidate API/UI checks only when a target surface, expected assertion, test data and cleanup strategy can be stated.",
        "- Keep exploratory public-web checks broad enough to cover navigation, representative pages, broken assets, console errors and accessibility basics when the task points at a public website.",
        "",
        "## Out of scope",
        "- Do not invent undocumented credentials, private data, or destructive write flows.",
        "- Do not turn vague visibility checks into executable tests without an explicit assertion target.",
        "",
        "## Known bugs",
        _markdown_list_or_default(
            _dedupe(_extract_known_bug_lines(parsed) + crawled_problems),
            "No known bugs were explicitly extracted from the intake bundle.",
        ),
        "",
        "## Relevant endpoints or pages",
        _markdown_list_or_default(surfaces, "No explicit endpoint or page path was detected; planner must discover surfaces during analysis."),
        "",
        "## Test data and credentials constraints",
        _markdown_list_or_default(constraints, "Use non-production data only. Confirm credentials and cleanup before approving generated candidates."),
        "",
        "## Open questions",
        _markdown_list_or_default(open_questions, "No open questions detected."),
        "",
    ]
    return "\n".join(lines)


def _synthesis_title(parsed: List[Dict[str, Any]], *, title: Optional[str]) -> str:
    if title and title.strip():
        return title.strip()[:160]
    if len(parsed) == 1:
        raw = str(parsed[0]["payload"].get("title") or "").strip()
        if raw:
            return f"{raw} - synthesized task"[:160]
    return f"Task intake from {len(parsed)} documents"


def _extract_requirement_lines(text: str, *, limit: int) -> List[str]:
    keywords = (
        "must", "should", "shall", "expected", "acceptance", "require",
        "feature", "user can", "user should", "verify", "validate",
        "error", "status", "bug", "scenario",
    )
    out: List[str] = []
    for raw in text.splitlines():
        if raw.lstrip().startswith("#"):
            continue
        line = _clean_markdown_line(raw)
        if not line or _is_metadata_line(line):
            continue
        lower = line.lower()
        is_bullet = raw.lstrip().startswith(("-", "*")) or re.match(r"^\s*\d+[\.)]\s+", raw)
        if is_bullet or any(k in lower for k in keywords):
            out.append(_clip(line, 260))
        if len(out) >= limit:
            break
    return _dedupe(out)


def _extract_surfaces(text: str, *, limit: int) -> List[str]:
    patterns = [
        r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%{}-]+)",
        r"\bhttps?://[^\s<>)\"']+",
        r"(?<![\w.-])/[A-Za-z0-9][A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%{}-]*",
    ]
    out: List[str] = []
    for method, path in re.findall(patterns[0], text, flags=re.IGNORECASE):
        out.append(f"{method.upper()} {path.rstrip('.,;')}")
    for pattern in patterns[1:]:
        for match in re.findall(pattern, text):
            value = match if isinstance(match, str) else " ".join(match)
            out.append(str(value).rstrip(".,;"))
            if len(out) >= limit:
                return _dedupe(out)[:limit]
    return _dedupe(out)[:limit]


def _extract_constraint_lines(text: str, *, limit: int) -> List[str]:
    keywords = (
        "credential", "login", "password", "token", "api key", "auth",
        "role", "test data", "fixture", "seed", "cleanup", "non-production",
        "sandbox", "readonly", "read-only",
    )
    out: List[str] = []
    for raw in text.splitlines():
        if raw.lstrip().startswith("#"):
            continue
        line = _clean_markdown_line(raw)
        if not line or _is_metadata_line(line):
            continue
        lower = line.lower()
        if any(k in lower for k in keywords):
            out.append(_clip(line, 240))
        if len(out) >= limit:
            break
    return _dedupe(out)


def _extract_known_bug_lines(parsed: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for item in parsed:
        text = str(item["payload"].get("spec_markdown") or "")
        for raw in text.splitlines():
            if raw.lstrip().startswith("#"):
                continue
            line = _clean_markdown_line(raw)
            if line and ("bug" in line.lower() or "defect" in line.lower()):
                out.append(_clip(line, 240))
    return _dedupe(out)[:12]
