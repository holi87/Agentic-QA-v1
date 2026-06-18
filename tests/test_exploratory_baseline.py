"""Exploratory baseline gating, safe-candidate generation, reporting, and cooldown behavior."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import yaml  # type: ignore

from agentic_os import exploratory as exp
from agentic_os.events import EventLog
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db

from test_notification_dispatch import _BASE_CONFIG  # type: ignore


def _repo(tmp_path: Path, autonomy=None):
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    cfg = copy.deepcopy(_BASE_CONFIG)
    if autonomy is not None:
        cfg["autonomy"] = autonomy
    (repo / "config" / "agentic-os.yml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return repo, cfg


# ---- safe bucket ----


def test_build_safe_candidates_shapes() -> None:
    items = exp.build_safe_candidates(
        routes=["/a", "/b"], openapi_gets=["/api/x"], wcag=True
    )
    kinds = [(i.test_type, i.title) for i in items]
    # 5 UI probes + axe (wcag) + 2 routes = 8 ui, + 1 api
    ui = [i for i in items if i.test_type == "ui"]
    api = [i for i in items if i.test_type == "api"]
    assert len(ui) == 8
    assert len(api) == 1
    assert any("axe" in i.title for i in ui)
    assert all(i.decision == "generate_now" for i in items)
    assert all(i.expected_assertion for i in items)


def test_safe_candidates_no_wcag_no_axe() -> None:
    items = exp.build_safe_candidates(routes=[], openapi_gets=[], wcag=False)
    assert not any("axe" in i.title for i in items)
    assert len(items) == 5  # just the UI probes


# ---- run baseline ----


def test_run_baseline_writes_report_and_generates(tmp_path: Path) -> None:
    repo, cfg = _repo(tmp_path)
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / "agentic-os-runtime")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    try:
        result = exp.run_exploratory_baseline(conn, paths, events, cfg, crawl_depth=1)
    finally:
        conn.close()
    # report always written (md + json)
    assert (repo / result.report_json).exists()
    assert (repo / result.report_md).exists()
    payload = json.loads((repo / result.report_json).read_text(encoding="utf-8"))
    assert payload["kind"] == "exploratory-baseline"
    # UI safe probes generated (no web URL → no crawl routes, no api)
    assert result.generated >= 5
    assert result.run_status == "skipped"  # no real runner script present
    # synthetic work item created
    assert result.work_item_id is not None


def test_run_baseline_report_written_even_when_run_fails(tmp_path: Path, monkeypatch) -> None:
    repo, cfg = _repo(tmp_path)
    # Point test_runner at an existing script that exits 1.
    runner = repo / "fail-runner.sh"
    runner.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    runner.chmod(0o755)
    cfg = copy.deepcopy(cfg)
    cfg["sut"]["test_runner"] = "fail-runner.sh"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / "agentic-os-runtime")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    try:
        result = exp.run_exploratory_baseline(conn, paths, events, cfg, crawl_depth=1)
    finally:
        conn.close()
    # Report must exist regardless of a non-zero run exit.
    assert (repo / result.report_json).exists()
    assert result.run_status in ("ran", "skipped")


# ---- gating (via autonomy) ----


def _session(preflight_ok=True):
    from agentic_os.autonomy import _SessionState
    from agentic_os.time_utils import now_iso

    return _SessionState(
        session_id="s", started_at=now_iso(), expected_finish_at=now_iso(),
        max_minutes=60, preflight={"ok": preflight_ok},
    )


def test_gate_flag_off_does_not_fire(tmp_path: Path) -> None:
    from agentic_os.autonomy import _maybe_exploratory_baseline

    repo, _cfg = _repo(tmp_path, autonomy={"exploratory_baseline": False})
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / "agentic-os-runtime")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    try:
        assert _maybe_exploratory_baseline(_session(), conn, paths, events) is False
    finally:
        conn.close()


def test_gate_fires_when_flag_on_and_preflight_ok(tmp_path: Path) -> None:
    from agentic_os.autonomy import _maybe_exploratory_baseline

    repo, _cfg = _repo(tmp_path, autonomy={"exploratory_baseline": True, "exploratory_crawl_depth": 1})
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / "agentic-os-runtime")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    sess = _session(preflight_ok=True)
    try:
        assert _maybe_exploratory_baseline(sess, conn, paths, events) is True
        # cooldown: a second immediate call is gated off
        assert _maybe_exploratory_baseline(sess, conn, paths, events) is False
    finally:
        conn.close()
    assert list((repo / "reports").glob("exploratory-baseline-*.json"))


def test_cooldown_survives_restart(tmp_path: Path) -> None:
    """A fresh session (in-memory marker reset) must still honour a recent
    on-disk baseline report — the cooldown is about completed runs, not memory."""
    from agentic_os.autonomy import _maybe_exploratory_baseline

    repo, _cfg = _repo(tmp_path, autonomy={
        "exploratory_baseline": True, "exploratory_cooldown_seconds": 3600,
    })
    # Simulate a recent prior run by dropping a report file.
    (repo / "reports").mkdir(parents=True, exist_ok=True)
    (repo / "reports" / "exploratory-baseline-20260526T000000Z.json").write_text(
        '{"kind":"exploratory-baseline"}', encoding="utf-8"
    )
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / "agentic-os-runtime")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    try:
        # Fresh session, no in-memory marker — disk mtime gates it off.
        assert _maybe_exploratory_baseline(_session(), conn, paths, events) is False
    finally:
        conn.close()


def test_gate_blocked_when_preflight_not_ok(tmp_path: Path) -> None:
    from agentic_os.autonomy import _maybe_exploratory_baseline

    repo, _cfg = _repo(tmp_path, autonomy={"exploratory_baseline": True})
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / "agentic-os-runtime")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    try:
        assert _maybe_exploratory_baseline(_session(preflight_ok=False), conn, paths, events) is False
    finally:
        conn.close()


# ---- config validation ----


def test_config_validates_exploratory_flags() -> None:
    from agentic_os.config import _validate

    cfg = copy.deepcopy(_BASE_CONFIG)
    cfg["autonomy"] = {
        "exploratory_baseline": True,
        "exploratory_crawl_depth": 3,
        "exploratory_cooldown_seconds": 1800,
    }
    assert _validate(cfg) == []
    cfg["autonomy"]["exploratory_crawl_depth"] = "deep"
    assert any("exploratory_crawl_depth" in e for e in _validate(cfg))


# ---- #317: online-only default-on exploratory baseline ----


def _online_repo(tmp_path: Path, autonomy=None):
    """Online-only repo: full base SUT block + online mode/web. The base
    ``test_runner`` path does not exist under tmp, so preflight reports a `warn`
    (not a `fail`) — the realistic online-only scenario."""
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    cfg = copy.deepcopy(_BASE_CONFIG)
    cfg["sut"] = {
        **copy.deepcopy(_BASE_CONFIG["sut"]),
        "mode": "online",
        "web": {"enabled": True, "url": "https://example.test"},
    }
    if autonomy is not None:
        cfg["autonomy"] = autonomy
    (repo / "config" / "agentic-os.yml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return repo, cfg


def _online_session(checks):
    from agentic_os.autonomy import _SessionState
    from agentic_os.time_utils import now_iso

    ok = all(c["status"] == "pass" for c in checks)
    return _SessionState(
        session_id="s", started_at=now_iso(), expected_finish_at=now_iso(),
        max_minutes=60, preflight={"ok": ok, "checks": checks},
    )


def test_exploratory_enabled_defaults() -> None:
    """Online-only defaults the baseline ON when the flag is unset; local stays
    opt-in (#238). An explicit value always wins."""
    from agentic_os.autonomy import _exploratory_enabled

    online = {"sut": {"mode": "online", "web": {"enabled": True, "url": "https://x.test"}}}
    local = {"sut": {"mode": "local", "root": "."}}
    assert _exploratory_enabled(online) is True
    assert _exploratory_enabled({**online, "autonomy": {"exploratory_baseline": False}}) is False
    assert _exploratory_enabled(local) is False
    assert _exploratory_enabled({**local, "autonomy": {"exploratory_baseline": True}}) is True


def test_gate_fires_online_default_despite_warn_preflight(tmp_path: Path, monkeypatch) -> None:
    """Online-only with only a URL (no test_runner -> preflight warn, ok=False)
    must still run the baseline — warns are non-blocking."""
    from agentic_os import autonomy as au

    repo, _cfg = _online_repo(tmp_path, autonomy={"exploratory_crawl_depth": 1})
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / "agentic-os-runtime")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    monkeypatch.setattr(exp, "_discover_routes", lambda url, depth: ["/"])
    sess = _online_session([
        {"id": "config", "status": "pass"},
        {"id": "test_runner", "status": "warn"},
    ])
    try:
        assert au._maybe_exploratory_baseline(sess, conn, paths, events) is True
    finally:
        conn.close()
    assert list((repo / "reports").glob("exploratory-baseline-*.json"))


def test_gate_blocked_online_when_preflight_has_fail(tmp_path: Path) -> None:
    """A hard preflight fail still blocks the online baseline."""
    from agentic_os import autonomy as au

    repo, _cfg = _online_repo(tmp_path)
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / "agentic-os-runtime")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    sess = _online_session([{"id": "sut_online", "status": "fail"}])
    try:
        assert au._maybe_exploratory_baseline(sess, conn, paths, events) is False
    finally:
        conn.close()


def test_online_pass_no_block_when_enabled(monkeypatch) -> None:
    """When exploratory is on (online default), the empty-queue online probe is a
    benign idle (None), not the deferred-synthesis block. Opt-out keeps the block."""
    from agentic_os import autonomy as au
    import agentic_os.crawler as crawler

    class _Rep:
        pages: list = []

    monkeypatch.setattr(crawler, "crawl_same_origin", lambda *a, **k: _Rep())
    cfg = {"sut": {"mode": "online", "web": {"enabled": True, "url": "https://x.test"}}}
    sess = au._SessionState(
        session_id="s", started_at=au._now_iso(),
        expected_finish_at=au._now_iso(), max_minutes=60,
    )
    assert au._online_exploratory_pass(sess, cfg, "https://x.test") is None
    cfg_off = {**cfg, "autonomy": {"exploratory_baseline": False}}
    assert au._online_exploratory_pass(sess, cfg_off, "https://x.test") == "online_task_synthesis_deferred"
