"""Document intake commands: inbox, crawler (issue #292)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..errors import AgenticOSError, ConfigError, InfraError, ProductFailure, UsageError, UserAbort
from ..orchestrator import (
    CURRENT_PHASE_ID,
    fetch_active_leases,
    fetch_bug_summary,
    fetch_last_run,
    fetch_phase_rows,
    fetch_task_summary,
    list_open_blockers,
    open_runtime,
)
from ..paths import detect_repo_root, runtime_paths_from_config
from ..storage.db import SCHEMA_NAME, SCHEMA_VERSION, transaction
from ..time_utils import now_iso
from ..security import require_safe_argv, resolve_repo_path
from ..analysis import analyze_work_item
from ..patch_builder import implement_tests_for_work_item
from ..test_planning import (
    plan_work_item,
    read_plan_candidates,
    approve_all_runnable_candidates,
    update_plan_candidate_decision,
)
from ..work_items import (
    annotate_spec_status,
    create_work_item_from_file,
    get_work_item_detail,
    link_work_items,
    list_work_items,
    prune_orphan_work_items,
)
from ..inbox import ingest_inbox, list_inbox_files, synthesize_inbox_task
from ..workflows import run_dry_run, run_final_gate, run_recovery, run_review_gate, run_tests
from ._state import _active_config_override


def cmd_inbox(repo_root: Path, args: List[str], *, json_output: bool) -> int:
    sub = argparse.ArgumentParser(prog="agentic-os inbox", add_help=True)
    sub.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=("list", "ingest", "synthesize"),
    )
    sub.add_argument(
        "--title",
        default=None,
        help="Title for `inbox synthesize`; defaults to a generated bundle title.",
    )
    opts = sub.parse_args(args)

    if opts.action == "list":
        paths = runtime_paths_from_config(repo_root)
        files = list_inbox_files(paths)
        if json_output:
            sys.stdout.write(json.dumps(
                {"files": [str(p.relative_to(repo_root)) for p in files]},
                indent=2,
                sort_keys=True,
            ) + "\n")
        elif not files:
            sys.stdout.write("(inbox empty)\n")
        else:
            sys.stdout.write("pending:\n")
            for f in files:
                sys.stdout.write(f"  - {f.relative_to(repo_root)}\n")
        return 0

    # ingest / synthesize
    conn, paths, events, _orch = open_runtime(repo_root)
    try:
        default_sut_root = _validated_config_sut_root(repo_root)
        if opts.action == "ingest":
            results = ingest_inbox(conn, paths, events, default_sut_root=default_sut_root)
        else:
            result = synthesize_inbox_task(
                conn,
                paths,
                events,
                title=opts.title,
                default_sut_root=default_sut_root,
            )
    finally:
        conn.close()
    if opts.action == "synthesize":
        if json_output:
            sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
        elif result["status"] == "empty":
            sys.stdout.write("(inbox empty)\n")
        elif result["status"] == "created":
            sys.stdout.write(
                f"created {result['work_item_id']} from {result['source_count']} document(s)\n"
                f"  title: {result['title']}\n"
                f"  spec:  {result['spec_path']}\n"
            )
            if result["failed"]:
                sys.stdout.write(f"  failed sources: {result['failed']}\n")
            for site in result.get("crawled_sites") or []:
                if site.get("status") == "ok":
                    sys.stdout.write(
                        f"  crawled: {site['start_url']} → "
                        f"{site['pages_visited']} pages, "
                        f"{site['broken_assets_total']} broken assets\n"
                    )
                else:
                    sys.stdout.write(
                        f"  crawl failed: {site.get('start_url') or site.get('source')} — "
                        f"{site.get('error') or 'unknown error'}\n"
                    )
        else:
            sys.stdout.write("failed to synthesize task\n")
            for r in result.get("results", []):
                if r.get("status") == "failed":
                    sys.stdout.write(f"  - {r['source']}: {r['error']}\n")
        return 0 if result["failed"] == 0 else 1

    if json_output:
        sys.stdout.write(json.dumps({"results": results}, indent=2, sort_keys=True) + "\n")
    elif not results:
        sys.stdout.write("(inbox empty)\n")
    else:
        for r in results:
            if r["status"] == "created":
                sys.stdout.write(
                    f"created {r['work_item_id']} from {r['source']}\n"
                    f"  title:    {r['title']}\n"
                    f"  archived: {r['archived_to']}\n"
                )
            else:
                sys.stdout.write(
                    f"failed  {r['source']}\n"
                    f"  error:    {r['error']}\n"
                    f"  archived: {r['archived_to']}\n"
                )
    failed = sum(1 for r in results if r["status"] == "failed")
    return 0 if failed == 0 else 1


def _validated_config_sut_root(repo_root: Path) -> str:
    from ..config import load_or_default

    cfg = load_or_default(repo_root, override=_active_config_override())
    sut = cfg.raw["sut"]
    sut_root = str(sut["root"])
    resolve_repo_path(repo_root, sut_root, label="sut.root", must_exist=False)
    require_safe_argv(sut["healthcheck"]["command"])
    return sut_root


def cmd_crawler(repo_root: Path, args: List[str], *, json_output: bool) -> int:
    """Same-origin route crawler for exploratory tests (issue #136).

    Deterministic HTTP crawl, no browser. Writes the report to ``--out``
    (default ``agentic-os-runtime/crawler/<host>.json``) and also prints
    a one-line summary on stdout for humans.
    """
    from ..crawler import crawl_report_to_json, crawl_report_to_str, crawl_same_origin

    sub = argparse.ArgumentParser(prog="agentic-os crawler", add_help=True)
    sub.add_argument("start_url", help="Origin to crawl from, e.g. https://example.com/")
    sub.add_argument("--depth", type=int, default=2, help="BFS depth limit (default: 2)")
    sub.add_argument("--max-pages", type=int, default=25, help="Hard cap on pages visited")
    sub.add_argument("--timeout", type=int, default=10, help="Per-request timeout (seconds)")
    sub.add_argument(
        "--user-agent",
        default="agentic-os-crawler/1.0",
        help="User-Agent header value",
    )
    sub.add_argument(
        "--no-robots",
        action="store_true",
        help="Ignore robots.txt (default: respect)",
    )
    sub.add_argument(
        "--allow-private",
        action="store_true",
        help="Allow loopback/RFC1918 targets — for local fixtures only",
    )
    sub.add_argument("--out", default=None, help="Override output path")
    sub.add_argument(
        "--browser",
        action="store_true",
        help=(
            "After the HTTP crawl, replay each route in headless Chromium "
            "to capture console.error / pageerror / requestfailed signals "
            "(issue #156). Requires Playwright + chromium installed."
        ),
    )
    opts = sub.parse_args(args)

    try:
        report = crawl_same_origin(
            opts.start_url,
            max_depth=opts.depth,
            max_pages=opts.max_pages,
            user_agent=opts.user_agent,
            timeout_seconds=opts.timeout,
            respect_robots=not opts.no_robots,
            allow_private=opts.allow_private,
        )
    except ValueError as exc:
        raise UsageError(str(exc))

    if opts.browser:
        from ..crawler_browser import PlaywrightUnavailable, enrich_with_browser_signals

        try:
            enrich_with_browser_signals(report)
        except PlaywrightUnavailable as exc:
            raise UsageError(str(exc))

    if opts.out:
        out_path = Path(opts.out).expanduser().resolve()
    else:
        from urllib.parse import urlparse

        paths = runtime_paths_from_config(repo_root)
        host = urlparse(opts.start_url).hostname or "site"
        out_dir = paths.runtime_root / "crawler"
        out_path = out_dir / f"{host}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    from ..atomic_io import atomic_write_json

    payload = crawl_report_to_json(report)
    atomic_write_json(out_path, payload)

    if json_output:
        sys.stdout.write(crawl_report_to_str(report) + "\n")
    else:
        summary = payload["summary"]
        lines = [
            f"crawled {summary['pages_visited']} pages from {report.origin}",
            f"  routes:           {summary['total_routes']}",
            f"  skipped (robots): {summary['pages_skipped_robots']}",
            f"  broken assets:    {summary['broken_assets_total']}",
        ]
        if report.browser_enriched:
            lines.extend([
                f"  console errors:   {summary['console_errors_total']}",
                f"  page errors:      {summary['page_errors_total']}",
                f"  failed requests:  {summary['failed_requests_total']}",
            ])
        lines.append(f"  report:           {out_path}")
        sys.stdout.write("\n".join(lines) + "\n")
    return 0
