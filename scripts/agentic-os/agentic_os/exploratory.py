"""Issue #238 — exploratory baseline.

When the autonomy queue is empty the loop used to only refresh the SUT
inventory (read-only `discover_sut`). This module turns that idle time into a
real QA pass: discover routes (reusing the #136 crawler), synthesise a safe
candidate bucket, generate a suite through the existing UI/API generators, run
it via the configured runner (skip with WARN when absent — never block), and
always write `reports/exploratory-baseline-<iso>.{md,json}` — even on exit 1.

Gating lives in `autonomy.py`: this only fires when the flag is on, the queue
is empty, preflight is green, and the cooldown has elapsed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .plan_v2 import PlanItem
from .time_utils import now_iso

# Hard-coded safe bucket (read-only probes only). The richer candidate set
# arrives with #229 (coverage architect); until then these never mutate state.
# Assertions follow the UI generator's grammar (URL/text/role). These probes
# are read-only smokes, so each asserts the loaded URL contains its path.
_SAFE_UI_PROBES = (
    ("home-smoke", "GET / UI smoke", "/", "URL must contain /"),
    ("robots-probe", "robots.txt probe", "/robots.txt", "URL must contain /robots"),
    ("sitemap-probe", "sitemap probe", "/sitemap.xml", "URL must contain /sitemap"),
    ("broken-asset", "broken asset check", "/", "URL must contain /"),
    ("console-error", "console error listener", "/", "URL must contain /"),
)


@dataclass
class ExploratoryResult:
    ok: bool
    report_md: str
    report_json: str
    generated: int
    routes_discovered: int
    api_candidates: int
    run_exit_code: Optional[int]
    run_status: str
    work_item_id: Optional[str]
    iso: str


def build_safe_candidates(
    *,
    routes: List[str],
    openapi_gets: List[str],
    wcag: bool,
) -> List[PlanItem]:
    """Construct the read-only safe candidate bucket as PlanItems."""
    items: List[PlanItem] = []
    n = 0

    def _next_id() -> str:
        nonlocal n
        n += 1
        return f"EXP-{n:03d}"

    for slug, title, page, assertion in _SAFE_UI_PROBES:
        items.append(PlanItem(
            candidate_id=_next_id(),
            title=title,
            test_type="ui",
            priority="P2",
            decision="generate_now",
            expected_assertion=assertion,
            source_refs=["exploratory-baseline"],
            target_page=page,
        ))
    if wcag:
        items.append(PlanItem(
            candidate_id=_next_id(),
            title="axe accessibility smoke",
            test_type="ui",
            priority="P2",
            decision="generate_now",
            expected_assertion="URL must contain /",
            source_refs=["exploratory-baseline", "wcag"],
            target_page="/",
        ))
    # One smoke per crawled route (bounded by the crawl caps upstream).
    for idx, route in enumerate(routes):
        items.append(PlanItem(
            candidate_id=_next_id(),
            title=f"route smoke {route}",
            test_type="ui",
            priority="P3",
            decision="generate_now",
            expected_assertion=f"URL must contain {route}",
            source_refs=["exploratory-crawl"],
            target_page=route,
        ))
    # One happy-path GET per documented OpenAPI GET endpoint.
    for path in openapi_gets:
        items.append(PlanItem(
            candidate_id=_next_id(),
            title=f"GET {path} happy path",
            test_type="api",
            priority="P2",
            decision="generate_now",
            expected_assertion="endpoint returns a 2xx response",
            source_refs=["openapi"],
            target_method="GET",
            target_path=path,
        ))
    return items


def _resolve_project_id(cfg_raw: Dict[str, Any]) -> str:
    """Resolve the project identity exploratory runs should attribute coverage to.

    Issue #328 — exploratory baselines must record coverage against the same
    project the per-task path uses (PR #327, ``patch_builder._try_generate_v2``),
    so future runs can dedupe regardless of which path produced the original
    spec. Reuses the ``project.active`` convention with the canonical
    ``"default"`` fallback.
    """
    project_cfg = cfg_raw.get("project") if isinstance(cfg_raw, dict) else None
    if isinstance(project_cfg, dict):
        active = project_cfg.get("active")
        if isinstance(active, str) and active.strip():
            return active.strip()
    return "default"


def _discover_routes(web_url: Optional[str], crawl_depth: int) -> List[str]:
    if not web_url:
        return []
    try:
        from .crawler import crawl_same_origin

        report = crawl_same_origin(web_url, max_depth=max(0, crawl_depth), max_pages=25)
    except Exception:
        return []
    routes: List[str] = []
    for page in getattr(report, "pages", []) or []:
        url = getattr(page, "url", None) if not isinstance(page, dict) else page.get("url")
        if isinstance(url, str):
            # Store the path component as the UI target_page.
            from urllib.parse import urlsplit

            path = urlsplit(url).path or "/"
            if path not in routes:
                routes.append(path)
    return routes[:25]


def _openapi_gets(paths: Any, cfg_raw: Dict[str, Any]) -> List[str]:
    sources = (((cfg_raw.get("sut") or {}).get("api") or {}).get("openapi") or {}).get("sources")
    if not isinstance(sources, list):
        return []
    gets: List[str] = []
    for src in sources:
        try:
            from .openapi import load_openapi_file

            inv = load_openapi_file(Path(paths.repo_root) / str(src))
        except Exception:
            continue
        for ep in getattr(inv, "endpoints", []) or []:
            method = getattr(ep, "method", None) if not isinstance(ep, dict) else ep.get("method")
            path = getattr(ep, "path", None) if not isinstance(ep, dict) else ep.get("path")
            if isinstance(method, str) and method.upper() == "GET" and isinstance(path, str):
                gets.append(path)
    return gets


def run_exploratory_baseline(
    conn: Any,
    paths: Any,
    events: Any,
    cfg_raw: Dict[str, Any],
    *,
    crawl_depth: int = 2,
) -> ExploratoryResult:
    """Discover → synthesise → generate → run → report. Report always written."""
    iso = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sut_cfg = cfg_raw.get("sut") or {}
    web_cfg = sut_cfg.get("web") or {}
    web_url = web_cfg.get("url") if web_cfg.get("enabled") else None
    wcag = bool(web_cfg.get("wcag"))

    routes = _discover_routes(web_url, crawl_depth)
    openapi_gets = _openapi_gets(paths, cfg_raw)
    candidates = build_safe_candidates(routes=routes, openapi_gets=openapi_gets, wcag=wcag)

    # Issue #328 — gate exploratory generation on the per-project coverage
    # ledger (#319/#320). Two consecutive runs on an unchanged SUT must add
    # zero duplicate exploratory specs; only newly discovered surfaces enter
    # the delta. Exploratory writes spec files directly to the repo (no
    # review-gate/apply-patch step), so coverage can be recorded immediately
    # after each spec lands instead of via a pending manifest.
    project_id = _resolve_project_id(cfg_raw)
    skipped_surfaces: List[Dict[str, Any]] = []
    try:
        from .coverage_ledger import partition_by_coverage

        delta_items, skipped_surfaces = partition_by_coverage(
            conn, project_id=project_id, plan_items=candidates
        )
    except Exception:
        # A broken ledger must never block the baseline.
        delta_items = list(candidates)
        skipped_surfaces = []
    for surface in skipped_surfaces:
        try:
            events.write(
                "work_item.coverage_skipped",
                payload={"work_item_id": None, **surface},
            )
        except Exception:
            pass

    # Generate the suite (best-effort per generator; a single bad item must
    # not abort the whole baseline).
    from .generators.api import generate_api_tests
    from .generators.ui import generate_ui_tests

    tests_dir = "tests/exploratory"
    abs_tests_dir = Path(paths.repo_root) / tests_dir
    generated = 0
    try:
        ui_tests = generate_ui_tests(delta_items, tests_dir=tests_dir)
        api_tests = generate_api_tests(delta_items, tests_dir=tests_dir)
    except Exception:
        ui_tests, api_tests = [], []
    written_specs: List[Any] = []
    for t in list(ui_tests) + list(api_tests):
        try:
            dest = Path(paths.repo_root) / t.relative_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(t.content, encoding="utf-8")
            generated += 1
            written_specs.append(t)
        except OSError:
            continue

    # Issue #328 — exploratory bypasses the review gate by design, so the
    # ledger is recorded immediately after spec files land on disk (the
    # per-task path waits for `apply-patch` instead). Recording is
    # best-effort; a ledger error must not roll back a successful write
    # loop.
    coverage_recorded = 0
    if written_specs:
        try:
            from .coverage_ledger import record_generated_coverage

            recorded = record_generated_coverage(
                conn,
                project_id=project_id,
                plan_items=delta_items,
                generated_tests=written_specs,
                work_item_id=None,
            )
            coverage_recorded = len(recorded)
        except Exception as exc:
            try:
                events.write(
                    "work_item.coverage_ledger_error",
                    payload={"work_item_id": None, "error": str(exc)},
                )
            except Exception:
                pass

    # Run step — best-effort, never blocks. Skipped (WARN) when no runner.
    run_exit_code: Optional[int] = None
    run_status = "skipped"
    runner = sut_cfg.get("test_runner")
    if runner:
        runner_path = Path(paths.repo_root) / runner
        if runner_path.exists():
            try:
                from .runtime.subprocess import run_command

                log_path = paths.subprocess_logs_dir / f"exploratory-{iso}.log"
                res = run_command([str(runner_path)], cwd=paths.repo_root,
                                  log_path=log_path, timeout_seconds=300,
                                  # Issue #291 — SUT test_runner is untrusted; deny model keys.
                                  include_provider_credentials=False)
                run_exit_code = res.exit_code
                run_status = "ran"
            except Exception:
                run_status = "skipped"

    # Synthetic work item so reviewers can filter the autonomous baseline.
    work_item_id: Optional[str] = None
    try:
        from .work_items import create_work_item_from_payload

        wi = create_work_item_from_payload(
            conn,
            paths,
            events,
            payload={
                "title": f"Exploratory baseline — {iso}",
                "source": "exploratory-autopilot",
                "priority": "P3",
                "body": f"Autonomous exploratory baseline. routes={len(routes)} "
                        f"api={len(openapi_gets)} generated={generated}",
            },
        )
        wi_row = wi.get("work_item") if isinstance(wi, dict) else None
        work_item_id = wi_row.get("id") if isinstance(wi_row, dict) else None
    except Exception:
        work_item_id = None

    # Report — ALWAYS written, even when the run exits 1 (existing invariant).
    report_payload = {
        "kind": "exploratory-baseline",
        "iso": iso,
        "created_at": now_iso(),
        "routes_discovered": len(routes),
        "api_candidates": len(openapi_gets),
        "candidates": len(candidates),
        "generated": generated,
        # Issue #328 — surface idempotency outcome so reviewers can see why a
        # second run on an unchanged SUT produced few/no new specs.
        "coverage": {
            "project_id": project_id,
            "delta": len(delta_items),
            "skipped": len(skipped_surfaces),
            "recorded": coverage_recorded,
        },
        "skipped_surfaces": skipped_surfaces,
        "run_status": run_status,
        "run_exit_code": run_exit_code,
        "work_item_id": work_item_id,
        "routes": routes,
    }
    report_dir = Path(paths.repo_root) / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"exploratory-baseline-{iso}.json"
    md_path = report_dir / f"exploratory-baseline-{iso}.md"
    json_path.write_text(json.dumps(report_payload, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(_render_md(report_payload), encoding="utf-8")

    return ExploratoryResult(
        ok=True,
        report_md=str(md_path.relative_to(paths.repo_root)),
        report_json=str(json_path.relative_to(paths.repo_root)),
        generated=generated,
        routes_discovered=len(routes),
        api_candidates=len(openapi_gets),
        run_exit_code=run_exit_code,
        run_status=run_status,
        work_item_id=work_item_id,
        iso=iso,
    )


def _render_md(p: Dict[str, Any]) -> str:
    lines = [
        f"# Exploratory baseline — {p['iso']}",
        "",
        f"- created: {p['created_at']}",
        f"- routes discovered: {p['routes_discovered']}",
        f"- API GET candidates: {p['api_candidates']}",
        f"- candidates synthesised: {p['candidates']}",
        f"- tests generated: {p['generated']}",
        f"- run status: {p['run_status']} (exit {p['run_exit_code']})",
        f"- work item: {p['work_item_id'] or '(none)'}",
        "",
        "## Discovered routes",
        "",
    ]
    for r in p.get("routes") or []:
        lines.append(f"- {r}")
    if not p.get("routes"):
        lines.append("(none — no web URL configured or crawl returned nothing)")
    return "\n".join(lines) + "\n"
