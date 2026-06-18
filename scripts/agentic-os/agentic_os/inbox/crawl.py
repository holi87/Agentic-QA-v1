"""Public-site crawling and crawl-report persistence.

Split from inbox.py (issue #292).
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..paths import RuntimePaths


def _crawl_public_sites(
    parsed: List[Dict[str, Any]],
    *,
    allow_private: bool,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """For each parsed intake doc tagged ``Type: public-site`` with a
    ``Start URL:``, run a shallow same-origin HTTP crawl. Returns
    ``(reports, summaries)`` where reports is the list passed to the
    synthesis renderer and summaries is the public-API roll-up persisted
    on the ``inbox.synthesized`` event.

    Crawl failures are recorded as a summary entry with ``error`` set but
    never abort synthesis — the intake doc still becomes a task; the
    operator simply sees the crawl couldn't reach the URL in the spec's
    Known bugs section.
    """
    from ..crawler import crawl_report_to_json, crawl_same_origin

    reports: List[Dict[str, Any]] = []
    summaries: List[Dict[str, Any]] = []
    for item in parsed:
        payload = item.get("payload") or {}
        if str(payload.get("intake_type") or "").lower() != INTAKE_TYPE_PUBLIC_SITE:
            continue
        start_url = payload.get("start_url")
        if not start_url:
            summaries.append({
                "source": item.get("source"),
                "start_url": None,
                "status": "skipped",
                "error": "intake doc tagged public-site is missing Start URL metadata",
            })
            continue
        try:
            report = crawl_same_origin(
                start_url,
                max_depth=_PUBLIC_SITE_CRAWL_DEPTH,
                max_pages=_PUBLIC_SITE_CRAWL_MAX_PAGES,
                allow_private=allow_private,
            )
        except ValueError as exc:
            summaries.append({
                "source": item.get("source"),
                "start_url": start_url,
                "status": "failed",
                "error": str(exc),
            })
            # Synthesize an error-shaped report so the renderer can still
            # surface the failure in Known bugs.
            reports.append({
                "start_url": start_url,
                "origin": "",
                "routes": [],
                "summary": {},
            })
            continue
        except Exception as exc:  # noqa: BLE001 — never abort synthesis
            summaries.append({
                "source": item.get("source"),
                "start_url": start_url,
                "status": "failed",
                "error": f"{exc.__class__.__name__}: {exc}",
            })
            reports.append({
                "start_url": start_url,
                "origin": "",
                "routes": [],
                "summary": {},
            })
            continue
        report_json = crawl_report_to_json(report)
        reports.append(report_json)
        summaries.append({
            "source": item.get("source"),
            "start_url": start_url,
            "status": "ok",
            "pages_visited": report_json["summary"]["pages_visited"],
            "total_routes": report_json["summary"]["total_routes"],
            "broken_assets_total": report_json["summary"]["broken_assets_total"],
        })
    return reports, summaries


def _persist_crawl_reports(
    paths: RuntimePaths,
    *,
    work_item_id: str,
    crawl_reports: List[Dict[str, Any]],
) -> List[str]:
    """Write each crawl JSON next to the work item so downstream analyze /
    plan stages can read structured data without re-parsing the spec
    markdown. Returns relative paths suitable for the event payload."""
    if not crawl_reports:
        return []
    from ..atomic_io import atomic_write_json

    out_dir = paths.runtime_root / "inbox" / "crawls" / work_item_id
    out_dir.mkdir(parents=True, exist_ok=True)
    written: List[str] = []
    for idx, report in enumerate(crawl_reports, start=1):
        out_path = out_dir / f"crawl-{idx:02d}.json"
        atomic_write_json(out_path, report)
        try:
            rel = str(out_path.relative_to(paths.repo_root))
        except ValueError:
            rel = str(out_path)
        written.append(rel)
    return written


INTAKE_TYPE_PUBLIC_SITE = "public-site"


_PUBLIC_SITE_CRAWL_DEPTH = 1


_PUBLIC_SITE_CRAWL_MAX_PAGES = 10
