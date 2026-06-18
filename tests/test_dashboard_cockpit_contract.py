"""Issue #202 — dashboard regression coverage umbrella.

The cockpit endpoints and the new queue filter UI ship together; both
need contracts that catch silent drift.

These tests complement:
  * `tests/test_dashboard_overview_api.py` — pure aggregation logic.
  * `tests/test_dashboard_no_remote_assets.py` — offline-safety.
  * `tests/test_dashboard_action_gating.py` — action prerequisites.
  * `tests/test_dashboard_candidate_debt.py` — done-while-pending.
  * `tests/test_dashboard_work_item_counters.py` — queue counters parity.

What this file adds:
  * HTTP-level smoke against the three new GET endpoints — proves the
    routes are wired, the JSON deserialises, and the shape matches the
    aggregation module.
  * HTML contract for the home page cockpit (every documented panel id
    is present in the served template).
  * HTML contract for the queue filter controls and lanes container.
  * Color-token check — the JS chart colors must resolve through CSS
    custom properties that are actually defined.
"""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.request
from pathlib import Path
from typing import Optional

import pytest

from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "scripts" / "agentic-os" / "templates"


# ---------------------------------------------------------------------------
# HTTP smoke for the three new endpoints
# ---------------------------------------------------------------------------


def _runtime(tmp_path: Path) -> tuple[sqlite3.Connection, RuntimePaths, EventLog, Orchestrator]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    return conn, paths, events, orch


def _start_server(paths: RuntimePaths):
    """Boot the dashboard HTTP server on a random local port."""
    httpd = make_server(paths, host="127.0.0.1", port=0)
    import threading

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


def _get_json(host: str, port: int, path: str) -> dict:
    url = f"http://{host}:{port}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body)


@pytest.fixture
def live_server(tmp_path):
    conn, paths, _events, _orch = _runtime(tmp_path)
    httpd, thread = _start_server(paths)
    host, port = httpd.server_address[0], httpd.server_address[1]
    try:
        yield {"paths": paths, "host": host, "port": port, "conn": conn}
    finally:
        httpd.shutdown()
        httpd.server_close()
        conn.close()


def test_overview_endpoint_serves_full_payload(live_server) -> None:
    payload = _get_json(live_server["host"], live_server["port"], "/api/dashboard/overview")
    for key in (
        "work_items",
        "candidates",
        "generated_tests",
        "latest_run",
        "current_process",
        "next_action",
        "bugs",
        "blockers",
    ):
        assert key in payload, f"missing key in /api/dashboard/overview: {key}"


def test_preflight_endpoint_returns_checks(live_server) -> None:
    payload = _get_json(live_server["host"], live_server["port"], "/api/dashboard/preflight")
    assert "ok" in payload
    assert isinstance(payload.get("checks"), list)
    ids = {c["id"] for c in payload["checks"]}
    # Dashboard-layer checks are always present even when autonomy
    # preflight fails fast.
    assert "runtime_db_integrity" in ids
    assert "dashboard_write_mode" in ids


def test_charts_endpoint_has_history_funnel_trend(live_server) -> None:
    payload = _get_json(live_server["host"], live_server["port"], "/api/dashboard/charts")
    assert "run_history" in payload and isinstance(payload["run_history"], list)
    assert "failure_trend" in payload
    assert "funnel" in payload and "planned" in payload["funnel"]


# ---------------------------------------------------------------------------
# HTML contract — home cockpit
# ---------------------------------------------------------------------------


def _home_html() -> str:
    return (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "element_id",
    [
        # Cockpit cards (#193 #196 #197 #199)
        "preflight-checks",
        "preflight-overall",
        "process-pill",
        "process-body",
        "next-pill",
        "next-action-body",
        "run-verdict",
        "run-summary-body",
        # Cockpit metrics (#193)
        "m-planned",
        "m-generated",
        "m-runs",
        "m-debt",
        # Charts (#195)
        "chart-history",
        "chart-funnel",
        "chart-trend",
        # Structured last-run (#197)
        "last-run-verdict",
        "lr-total",
        "lr-passed",
        "lr-failed",
        "lr-skipped",
        "lr-known-bug",
    ],
)
def test_home_template_carries_cockpit_id(element_id: str) -> None:
    html = _home_html()
    assert f'id="{element_id}"' in html, (
        f"home template missing #{element_id} — cockpit JS would render to /dev/null"
    )


def test_home_template_calls_cockpit_bootstrap() -> None:
    html = _home_html()
    assert "AgenticOS.startCockpitPolling" in html, (
        "cockpit polling not booted on home page"
    )


# ---------------------------------------------------------------------------
# HTML contract — queue filters (#198)
# ---------------------------------------------------------------------------


def _tasks_html() -> str:
    return (TEMPLATES_DIR / "tasks_list.html").read_text(encoding="utf-8")


def _task_detail_html() -> str:
    return (TEMPLATES_DIR / "tasks_detail.html").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "element_id",
    [
        "queue-search",
        "queue-filter-status",
        "queue-filter-priority",
        "queue-view-mode",
        "queue-table-view",
        "queue-lanes-view",
    ],
)
def test_queue_controls_in_tasks_list(element_id: str) -> None:
    html = _tasks_html()
    assert f'id="{element_id}"' in html, (
        f"tasks_list.html missing #{element_id} — filter UI would not render"
    )


def test_task_detail_exposes_apply_patch_step_and_action() -> None:
    html = _task_detail_html()
    assert 'data-step="apply-patch"' in html
    assert 'id="action-apply-patch"' in html
    assert 'id="generated-specs-summary"' in html
    assert 'id="generated-specs-list"' in html
    js = _js()
    assert "{ kind: 'apply-patch'" in js
    assert "'apply-patch':" in js
    assert "renderGeneratedTests(detail.generated_tests || [])" in js


def test_cockpit_next_action_uses_current_task_route() -> None:
    js = _js()
    assert "link.href = '/tasks/' + encodeURIComponent(nxt.work_item_id);" in js
    assert "link.href = '/task/' + nxt.work_item_id;" not in js


def test_dashboard_runtime_tables_have_explicit_empty_states() -> None:
    html = _home_html()
    js = _js()
    assert "(loading leases)" in html
    assert "(no active leases)" in js
    assert "counts unavailable" in js


# ---------------------------------------------------------------------------
# Color-token integrity (#201)
# ---------------------------------------------------------------------------


def _css() -> str:
    return (TEMPLATES_DIR / "static" / "dashboard.css").read_text(encoding="utf-8")


def _js() -> str:
    return (TEMPLATES_DIR / "static" / "dashboard.js").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "token",
    ["--c-success", "--c-warn", "--c-danger", "--c-infra", "--c-info",
     "--c-accent", "--c-known", "--c-muted"],
)
def test_color_tokens_defined_in_css(token: str) -> None:
    css = _css()
    assert re.search(rf"^\s*{re.escape(token)}\s*:", css, re.M), (
        f"color token {token} is referenced by the cockpit JS but missing from dashboard.css"
    )


@pytest.mark.parametrize(
    "token",
    ["--c-success", "--c-warn", "--c-danger", "--c-infra", "--c-info",
     "--c-accent", "--c-known"],
)
def test_color_tokens_used_by_cockpit_js(token: str) -> None:
    js = _js()
    assert token in js, (
        f"color token {token} is defined in CSS but never referenced by the JS — "
        "either remove the token or wire it into a chart/pill"
    )
