"""Exploratory-baseline passes (offline + online) (issue #292)."""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set

from .. import task_synthesis
from ..events import EventLog, event_log_for_paths
from ..paths import RuntimePaths
from ..runtime.tuning import (
    EVENTS_LOG_RING_SIZE as _EVENTS_LOG_RING_SIZE,
    RECORD_DETAIL_MAX_CHARS as _RECORD_DETAIL_MAX_CHARS,
    SHUTDOWN_GRACE_SECONDS as _SHUTDOWN_GRACE_SECONDS,
)
from ..storage import init_db
from .session_state import _SessionState, _record



def _online_endpoint_urls(cfg_raw: Dict[str, Any]) -> Dict[str, str]:
    sut = (cfg_raw.get("sut") or {}) if isinstance(cfg_raw, dict) else {}
    if sut.get("mode") != "online":
        return {}
    urls: Dict[str, str] = {}
    for surface in ("web", "api"):
        surface_cfg = sut.get(surface) if isinstance(sut.get(surface), dict) else {}
        url = surface_cfg.get("url") if surface_cfg.get("enabled") else None
        if isinstance(url, str) and url.strip():
            urls[surface] = url.strip()
    return urls

def _online_web_url(cfg_raw: Dict[str, Any]) -> Optional[str]:
    return _online_endpoint_urls(cfg_raw).get("web")

def _exploratory_enabled(cfg_raw: Dict[str, Any]) -> bool:
    """Issue #317 — is the exploratory baseline active for this config?

    The ``autonomy.exploratory_baseline`` flag is opt-in for local SUTs (#238).
    For an online-only SUT (a web URL is all the operator configured) it
    defaults **on** when the flag is left unset, so an empty queue produces
    exploratory tests instead of an ``idle:blocked`` deferral. An explicit
    value (true or false) always wins.
    """
    autonomy_cfg = (cfg_raw.get("autonomy") or {}) if isinstance(cfg_raw, dict) else {}
    explicit = autonomy_cfg.get("exploratory_baseline")
    if explicit is None:
        return bool(_online_web_url(cfg_raw))
    return bool(explicit)

def _latest_baseline_age_seconds(paths: RuntimePaths) -> Optional[float]:
    """Seconds since the newest exploratory-baseline report, or None if absent."""
    reports_dir = paths.repo_root / "reports"
    if not reports_dir.exists():
        return None
    newest: Optional[float] = None
    for path in reports_dir.glob("exploratory-baseline-*.json"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    if newest is None:
        return None
    return max(0.0, time.time() - newest)

def _maybe_exploratory_baseline(
    session: _SessionState,
    conn: Any,
    paths: RuntimePaths,
    events: EventLog,
) -> bool:
    """Issue #238 — fire the exploratory baseline when ALL gates pass.

    Gates: ``autonomy.exploratory_baseline`` on, queue empty (caller already
    verified), preflight ``ok``, and the cooldown elapsed since the last
    baseline. Returns True when a baseline ran. Never raises out — failures
    record and return False so the idle loop keeps probing.
    """
    from ..config import ConfigError, load_or_default

    try:
        cfg = load_or_default(paths.repo_root)
    except ConfigError:
        return False
    autonomy_cfg = (cfg.raw.get("autonomy") or {}) if isinstance(cfg.raw, dict) else {}
    if not _exploratory_enabled(cfg.raw):
        return False
    # Preflight gate. For an online-only SUT a missing test_runner is only a
    # `warn` (the baseline run-step skips with WARN, never blocks), so warns
    # must not stop generation — only a hard `fail` does. Local SUTs keep the
    # stricter all-pass requirement (#238).
    preflight = session.preflight or {}
    if _online_web_url(cfg.raw):
        if any(c.get("status") == "fail" for c in preflight.get("checks", [])):
            return False
    elif not preflight.get("ok"):
        return False
    cooldown = autonomy_cfg.get("exploratory_cooldown_seconds", 3600)
    cooldown = int(cooldown) if isinstance(cooldown, int) else 3600
    last = getattr(session, "_last_exploratory_baseline_at", None)
    now_mono = time.monotonic()
    if last is not None and (now_mono - last) < cooldown:
        return False
    # Cooldown must survive a worker restart: an in-memory marker resets to
    # None on restart, so also honour the newest baseline report on disk.
    disk_age = _latest_baseline_age_seconds(paths)
    if disk_age is not None and disk_age < cooldown:
        return False
    crawl_depth = autonomy_cfg.get("exploratory_crawl_depth", 2)
    crawl_depth = int(crawl_depth) if isinstance(crawl_depth, int) else 2
    try:
        from ..exploratory import run_exploratory_baseline

        result = run_exploratory_baseline(conn, paths, events, cfg.raw, crawl_depth=crawl_depth)
    except Exception as exc:  # intentionally broad: baseline crawl spans subprocess/fs/parse — record and keep the idle loop probing
        _record(session, "exploratory:baseline", False, f"failed: {exc}")
        return False
    try:
        session._last_exploratory_baseline_at = now_mono  # type: ignore[attr-defined]
    except Exception:
        pass
    _record(
        session,
        "exploratory:baseline",
        True,
        f"generated={result.generated} routes={result.routes_discovered} "
        f"api={result.api_candidates} run={result.run_status} report={result.report_json}",
    )
    return True

def _exploratory_pass(session: _SessionState, paths: RuntimePaths) -> Optional[str]:
    """Best-effort discovery sweep over the configured SUT.

    Runs while the queue is empty so the operator can see autonomy is
    still doing useful work (refreshing the SUT picture) rather than
    silently sleeping. Bounded by `discover_sut`'s max_files cap; no
    persistence side-effects.

    Returns a signal string the run-loop uses to detect persistently
    misconfigured runs: ``"stack_unknown"`` when the SUT yields no
    markers and an unknown stack, otherwise ``None``.
    """
    from ..config import ConfigError, load_or_default
    from ..sut_discovery import discover_sut

    try:
        cfg = load_or_default(paths.repo_root)
    except ConfigError as exc:
        _record(session, "exploratory", False, f"config invalid: {exc}")
        return "config_invalid"
    online_url = _online_web_url(cfg.raw)
    if online_url:
        return _online_exploratory_pass(session, cfg.raw, online_url)

    sut_root_str = (cfg.raw.get("sut") or {}).get("root") or "."
    sut_root = (paths.repo_root / sut_root_str).resolve()
    if not sut_root.exists():
        _record(session, "exploratory", False, f"sut_root missing: {sut_root}")
        return "sut_root_missing"
    # Issue #241 — opt-in git ensure once per session, attached to the
    # preflight discovery pass so the autopilot self-bootstraps the SUT
    # repo when `git.enabled=true`.
    git_cfg = (cfg.raw.get("git") or {}) if isinstance(cfg.raw, dict) else {}
    if bool(git_cfg.get("enabled")) and not getattr(session, "_git_ensured", False):
        try:
            from ..sut_repo import git_ensure
            from ..storage.db import init_db

            conn = init_db(paths.db)
            try:
                events = event_log_for_paths(conn, paths)
                report = git_ensure(
                    paths,
                    events,
                    git_config=git_cfg,
                    sut_root=sut_root_str,
                )
                _record(
                    session,
                    "git_ensure",
                    report.ok,
                    f"{report.summary} ({len(report.ops)} ops)",
                )
            finally:
                conn.close()
        except Exception as exc:  # pragma: no cover - best-effort hook
            _record(session, "git_ensure", False, f"failed: {exc}")
        try:
            session._git_ensured = True  # type: ignore[attr-defined]
        except Exception:
            pass
    result = discover_sut(sut_root)
    marker_count = sum(len(v) for v in (result.markers or {}).values())
    detail = (
        f"stack={result.stack} tests={len(result.tests)} markers={marker_count}"
    )
    _record(session, "exploratory", True, detail)
    if result.stack == "unknown" and marker_count == 0:
        return "stack_unknown"
    return None

def _online_exploratory_pass(
    session: _SessionState,
    cfg_raw: Dict[str, Any],
    web_url: str,
) -> Optional[str]:
    """Probe the saved online Web URL instead of local ``sut.root``.

    The richer empty-queue synthesis belongs to #290. Until that lands, this
    path proves the dashboard is using the online URL and then exposes a clear
    block reason instead of looping as if local source discovery were useful.
    """
    autonomy_cfg = (cfg_raw.get("autonomy") or {}) if isinstance(cfg_raw, dict) else {}
    crawl_depth = autonomy_cfg.get("exploratory_crawl_depth", 2)
    crawl_depth = int(crawl_depth) if isinstance(crawl_depth, int) else 2
    try:
        from urllib.parse import urlsplit

        from ..crawler import crawl_same_origin

        report = crawl_same_origin(web_url, max_depth=max(0, crawl_depth), max_pages=25)
        routes: List[str] = []
        for page in getattr(report, "pages", []) or []:
            page_url = getattr(page, "url", None) if not isinstance(page, dict) else page.get("url")
            if isinstance(page_url, str):
                path = urlsplit(page_url).path or "/"
                if path not in routes:
                    routes.append(path)
    except Exception as exc:
        _record(session, "exploratory:online", False, f"url={web_url} crawl failed: {exc}")
        return "online_crawl_failed"

    _record(
        session,
        "exploratory:online",
        True,
        f"url={web_url} routes={len(routes)} source=sut.web.url",
    )
    # Issue #317 — when the exploratory baseline is active (online-only defaults
    # it on), the baseline drives generation and this probe is a benign idle
    # liveness check: return None so the loop keeps polling instead of recording
    # a block. The deferred-synthesis block reason is reserved for operators who
    # explicitly opted out of exploratory baselining on an online SUT.
    if _exploratory_enabled(cfg_raw):
        return None
    return "online_task_synthesis_deferred"
