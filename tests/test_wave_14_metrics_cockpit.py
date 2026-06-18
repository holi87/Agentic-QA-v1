"""Wave 14 (#314) — unified metrics rollup, /api/metrics endpoint,
Prometheus exposition, and cockpit page.

Goals proved by these tests:
* ``build_metrics`` aggregates the seven KPI buckets and survives missing
  tables (returns zeros, never raises).
* ``render_prometheus`` emits valid exposition-format text with labeled
  series for per-provider / per-surface / per-kind dimensions.
* The HTTP dispatcher serves ``/api/metrics``, ``/metrics``, and
  ``/metrics-cockpit`` with the right content types and the nav link
  appears on the cockpit shell.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import urllib.request
from pathlib import Path

import pytest

from agentic_os.events import EventLog
from agentic_os.metrics import build_metrics, render_prometheus
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db
from agentic_os.storage.db import connect
from agentic_os.work_items import create_work_item_from_payload

from test_dashboard_task_ui import _DEFAULT_CONFIG, _free_port, _wait


def _runtime(tmp_path: Path) -> tuple[sqlite3.Connection, RuntimePaths]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    cfg = repo / ".qualitycat" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_DEFAULT_CONFIG.format(write="false").lstrip(), encoding="utf-8")
    conn = init_db(paths.db)
    Orchestrator(conn, paths, EventLog(conn, paths)).seed_phases()
    return conn, paths


# ---------------------------------------------------------------------------
# build_metrics — math + empty-state behavior
# ---------------------------------------------------------------------------


def test_build_metrics_empty_db_returns_zeros(tmp_path: Path) -> None:
    conn, paths = _runtime(tmp_path)
    try:
        m = build_metrics(conn, paths)
        assert m["tests"]["work_items_total"] == 0
        assert m["coverage"]["total_rows"] == 0
        assert m["cost"]["totals"]["cost_usd"] == 0.0
        assert m["sessions"]["totals"]["sessions"] == 0
        assert m["blocks"]["total_blocks"] == 0
        # Every component returns the expected shape even on empty data.
        for key in ("tests", "coverage", "cost", "providers", "blocks", "phase_timing"):
            assert key in m
        assert m["generated_at"], "generated_at must be populated"
    finally:
        conn.close()


def test_build_metrics_counts_work_items_and_invocations(tmp_path: Path) -> None:
    conn, paths = _runtime(tmp_path)
    try:
        events = EventLog(conn, paths)
        create_work_item_from_payload(
            conn,
            paths,
            events,
            {
                "title": "demo",
                "priority": "P1",
                "business_goal": "demo",
                "expected_behavior": "demo",
                "in_scope": "demo",
                "out_of_scope": "demo",
                "known_bugs": "demo",
                "relevant_surfaces": "demo",
                "test_data": "demo",
                "time_budget": "60",
            },
            default_sut_root=".",
        )
        # Seed a couple of model_invocations rows directly — the DB schema
        # is the contract, and this is enough to prove the SQL rollup.
        conn.execute(
            """
            INSERT INTO model_invocations(
              id, task_id, run_id, model_role, provider, command,
              input_path, output_path, exit_code, started_at, finished_at,
              session_id, provider_version, tokens_in, tokens_out, cost_usd
            ) VALUES (?, NULL, NULL, ?, ?, ?, NULL, NULL, 0,
                      '2026-05-28T00:00:00Z', '2026-05-28T00:00:05Z',
                      ?, 'v1', ?, ?, ?);
            """,
            ("inv-1", "sonnet", "claude", '["claude"]', "sess-A", 1000, 500, 0.123),
        )
        conn.execute(
            """
            INSERT INTO model_invocations(
              id, task_id, run_id, model_role, provider, command,
              input_path, output_path, exit_code, started_at, finished_at,
              session_id, provider_version, tokens_in, tokens_out, cost_usd
            ) VALUES (?, NULL, NULL, ?, ?, ?, NULL, NULL, 0,
                      '2026-05-28T00:00:00Z', '2026-05-28T00:00:05Z',
                      ?, 'v1', ?, ?, ?);
            """,
            ("inv-2", "codex", "codex", '["codex"]', "sess-A", 200, 100, 0.045),
        )
        conn.commit()

        m = build_metrics(conn, paths)
        assert m["tests"]["work_items_total"] == 1
        assert m["cost"]["totals"]["invocations"] == 2
        assert m["cost"]["totals"]["tokens_in"] == 1200
        assert m["cost"]["totals"]["tokens_out"] == 600
        assert m["cost"]["totals"]["cost_usd"] == pytest.approx(0.168, rel=1e-3)
        providers = {row["provider"]: row for row in m["cost"]["by_provider"]}
        assert "claude" in providers and "codex" in providers
        # Two providers handled the same role? No — different roles. But
        # the failover heuristic counts roles with >1 provider; with this
        # seed each role has one provider, so failover_events = 0.
        assert m["providers"]["failover_events"] == 0
    finally:
        conn.close()


def test_build_metrics_failover_when_role_spans_providers(tmp_path: Path) -> None:
    conn, paths = _runtime(tmp_path)
    try:
        # Two invocations on the same role from different providers — the
        # failover heuristic should fire.
        for i, provider in enumerate(("claude", "codex")):
            conn.execute(
                """
                INSERT INTO model_invocations(
                  id, task_id, run_id, model_role, provider, command,
                  input_path, output_path, exit_code, started_at, finished_at
                ) VALUES (?, NULL, NULL, 'sonnet', ?, ?, NULL, NULL, 0,
                          '2026-05-28T00:00:00Z', '2026-05-28T00:00:05Z');
                """,
                (f"inv-{i}", provider, f'["{provider}"]'),
            )
        conn.commit()
        m = build_metrics(conn, paths)
        assert m["providers"]["failover_events"] >= 1
    finally:
        conn.close()


def test_build_metrics_coverage_groups_by_project_and_kind(tmp_path: Path) -> None:
    conn, paths = _runtime(tmp_path)
    try:
        from agentic_os.coverage_ledger import record_coverage

        record_coverage(
            conn,
            project_id="default",
            surface_kind="api",
            surface_key="GET /foo",
            assertion_kind="status",
            spec_path="tests/api/a.spec.ts",
        )
        record_coverage(
            conn,
            project_id="default",
            surface_kind="ui",
            surface_key="/checkout",
            assertion_kind="visible",
            spec_path="tests/ui/c.spec.ts",
        )
        conn.commit()
        m = build_metrics(conn, paths)
        assert m["coverage"]["total_rows"] == 2
        assert m["coverage"]["by_surface_kind"] == {"api": 1, "ui": 1}
        assert any(p["project_id"] == "default" for p in m["coverage"]["by_project"])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Prometheus exporter
# ---------------------------------------------------------------------------


def test_prometheus_render_includes_required_series() -> None:
    sample = {
        "generated_at": "2026-05-28T00:00:00Z",
        "tests": {
            "work_items_total": 4,
            "outcomes": {"pass": 2, "product": 1, "unknown": 0},
            "patches_applied": 3,
        },
        "coverage": {"total_rows": 7, "by_surface_kind": {"api": 4, "ui": 3}},
        "cost": {
            "totals": {"tokens_in": 1200, "tokens_out": 600, "cost_usd": 0.42, "invocations": 5},
            "by_provider": [{"provider": "claude", "cost_usd": 0.3, "n": 3}],
            "by_role": [],
            "by_session": [],
        },
        "providers": {
            "failover_events": 2,
            "active_cooldowns": [],
            "by_provider_outcomes": [],
        },
        "blocks": {"total_blocks": 1, "by_reason": []},
        "sessions": {"recent": [], "totals": {"sessions": 1, "blocks": 0, "failures": 0}},
        "phase_timing": {
            "by_kind": [{"kind": "review", "n": 2, "avg_ms": 1234.5, "min_ms": 1000, "max_ms": 1500}]
        },
    }
    text = render_prometheus(sample)
    assert "agentic_os_work_items_total 4" in text
    assert 'agentic_os_runs_outcomes{outcome="pass"} 2' in text
    assert "agentic_os_cost_usd_total 0.42" in text
    assert 'agentic_os_cost_usd{provider="claude"} 0.3' in text
    assert "agentic_os_provider_failover_events 2" in text
    assert 'agentic_os_phase_duration_ms_avg{kind="review"} 1234.5' in text
    # No malformed metric without a value — every series line ends in a
    # parseable float (incl. scientific notation for very small cost_usd).
    for line in text.splitlines():
        if line.startswith("agentic_os_") and not line.startswith("#"):
            float(line.rsplit(" ", 1)[-1])


def test_prometheus_render_escapes_label_values() -> None:
    sample = {
        "providers": {
            "by_provider_outcomes": [],
            "active_cooldowns": [],
            "failover_events": 0,
        },
        "tests": {"outcomes": {"weird\"name": 1, "with\\backslash": 2}},
        "cost": {"totals": {}, "by_provider": [], "by_role": [], "by_session": []},
        "coverage": {"by_surface_kind": {}},
        "blocks": {},
        "sessions": {"totals": {}},
        "phase_timing": {"by_kind": []},
    }
    text = render_prometheus(sample)
    assert 'outcome="weird\\"name"' in text
    assert 'outcome="with\\\\backslash"' in text


# ---------------------------------------------------------------------------
# HTTP dispatcher — /api/metrics, /metrics, /metrics-cockpit
# ---------------------------------------------------------------------------


@pytest.fixture
def metrics_server(tmp_path: Path):
    conn, paths = _runtime(tmp_path)
    conn.close()
    server = make_server(paths, host="127.0.0.1", port=_free_port())
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    # Health probe — ensures the listener accepted before the test issues
    # real requests.
    _wait(base + "/healthz").close()
    try:
        yield paths, base
    finally:
        server.shutdown()
        server.server_close()


def test_api_metrics_endpoint_serves_rollup(metrics_server) -> None:
    paths, base = metrics_server
    with urllib.request.urlopen(base + "/api/metrics", timeout=5) as r:
        assert r.status == 200
        assert r.headers["Content-Type"].startswith("application/json")
        payload = json.loads(r.read().decode("utf-8"))
    for key in ("tests", "coverage", "cost", "providers", "blocks", "phase_timing", "sessions"):
        assert key in payload, key
    assert payload["generated_at"]


def test_prometheus_endpoint_serves_exposition_format(metrics_server) -> None:
    paths, base = metrics_server
    with urllib.request.urlopen(base + "/metrics", timeout=5) as r:
        assert r.status == 200
        assert r.headers["Content-Type"].startswith("text/plain")
        body = r.read().decode("utf-8")
    # An empty-DB rollup still produces the headers + zero-valued series.
    assert "agentic_os_work_items_total" in body
    assert body.endswith("\n")


def test_cockpit_page_serves_html_and_carries_nav(metrics_server) -> None:
    paths, base = metrics_server
    with urllib.request.urlopen(base + "/metrics-cockpit", timeout=5) as r:
        assert r.status == 200
        assert r.headers["Content-Type"].startswith("text/html")
        html = r.read().decode("utf-8")
    # Nav renders the new Metrics link.
    assert 'href="/metrics-cockpit"' in html
    assert "Metrics" in html
    # Cockpit pulls /api/metrics in JS.
    assert "/api/metrics" in html
    # Security guard — no `.innerHTML =` / `innerHTML+=` assignments on
    # API-derived strings (a comment mentioning innerHTML in passing is
    # fine, hence the assignment-shaped match).
    import re

    assert not re.search(r"\.innerHTML\s*[+]?=", html), (
        "metrics cockpit must not assign innerHTML — DB strings are operator-supplied"
    )
