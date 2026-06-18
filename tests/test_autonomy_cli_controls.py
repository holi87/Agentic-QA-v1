"""Autonomy CLI controls, completion, reports, budget, and cooldown endpoint contracts."""
from __future__ import annotations

import builtins
import json
import sys
import threading
import time as _time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentic_os.cli import (
    cmd_autonomy,
    cmd_budget,
    cmd_doctor,
    cmd_reports,
    cmd_verifications,
)
from agentic_os.decisions import record_decision
from agentic_os.events import EventLog
from agentic_os.orchestrator import CURRENT_PHASE_ID, Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db
from agentic_os.time_utils import now_iso


_MINIMAL_CONFIG = """\
runtime:
  root: agentic-os-runtime
  timezone: Europe/Warsaw
  max_parallel_tasks: 1
  heartbeat_seconds: 10
  lease_ttl_seconds: 600
  stale_lease_seconds: 1800
  shutdown_grace_seconds: 30
  timeouts:
    default_seconds: 600
    docker_seconds: 120
    test_seconds: 900
    model_seconds: 600
    report_seconds: 120

sut:
  root: .
  compose_file: docker-compose.yml
  compose_project_name: app
  autostart: false
  healthcheck:
    command: ["sh", "-c", "exit 0"]
    timeout_seconds: 5
    retries: 1
  test_runner: scripts/run-tests.sh
  install_shim_allowed: false

models:
  planner:
    provider: claude
    command: ["claude", "--model", "opus"]
    role: opus
  implementer:
    provider: claude
    command: ["claude", "--model", "sonnet"]
    role: sonnet
  reviewer:
    provider: codex
    command: ["codex"]
    role: codex

budgets:
  fail_mode: warn
  session:
    max_tokens: 1000
    max_usd: 5.0
  per_role:
    planner:
      max_tokens: 500

dashboard:
  host: 127.0.0.1
  port: 8765
  enable_write_endpoints: false

paths:
  reports: reports
  bugs: bugs
  evidence: evidence
  prompts: prompts

reports:
  copy_reports_script: scripts/copy-reports.sh
  extract_last_run_script: scripts/extract-last-run.sh
  build_summary_script: scripts/build-summary.sh
  require_reports_on_failure: true

gates:
  known_bugs_fail_exit: true
  assertion_changes_require_decision: true
  exact_spec_failure_opens_bug: true
  require_functional_area_tag: true
  require_lifecycle_tag: true
  infrastructure_exit_code: 2
"""


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    cfg = repo / "config" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_MINIMAL_CONFIG.format(repo=str(repo)), encoding="utf-8")
    return repo


# ---------------------------------------------------------------------------
# pause / resume worker semantics
# ---------------------------------------------------------------------------


def test_wait_if_paused_flips_status_and_records() -> None:
    from agentic_os.autonomy import _SessionState, _wait_if_paused

    sess = _SessionState(
        session_id="sx",
        started_at=now_iso(),
        expected_finish_at=now_iso(),
        max_minutes=1,
        status="running",
    )
    stop = threading.Event()
    pause = threading.Event()
    pause.set()
    t = threading.Thread(target=_wait_if_paused, args=(sess, stop, pause))
    t.start()
    deadline = _time.time() + 3
    while sess.status != "paused" and _time.time() < deadline:
        _time.sleep(0.02)
    assert sess.status == "paused"
    assert sess.paused_at is not None
    pause.clear()
    t.join(timeout=2)
    assert sess.status == "running"
    assert sess.paused_at is None
    steps = [e["step"] for e in sess.events_log]
    assert "session.paused" in steps
    assert "session.resumed" in steps


def test_wait_if_paused_exits_on_stop() -> None:
    """A paused worker must observe stop and exit instead of parking forever."""
    from agentic_os.autonomy import _SessionState, _wait_if_paused

    sess = _SessionState(
        session_id="sy",
        started_at=now_iso(),
        expected_finish_at=now_iso(),
        max_minutes=1,
        status="running",
    )
    stop = threading.Event()
    pause = threading.Event()
    pause.set()
    t = threading.Thread(target=_wait_if_paused, args=(sess, stop, pause))
    t.start()
    _time.sleep(0.1)
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()


def test_cli_pause_resume_no_session(repo_root: Path, capsys) -> None:
    rc = cmd_autonomy(repo_root, ["pause", "--json"], json_output=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload.get("active") in (False, None)
    rc = cmd_autonomy(repo_root, ["resume", "--json"], json_output=True)
    assert rc == 0


# ---------------------------------------------------------------------------
# verifications
# ---------------------------------------------------------------------------


def test_verifications_list_and_show(repo_root: Path, capsys) -> None:
    from agentic_os.orchestrator import open_runtime

    conn, paths, events, _o = open_runtime(repo_root)
    try:
        Orchestrator(conn, paths, events).seed_phases()
        did = record_decision(
            conn,
            phase_id=CURRENT_PHASE_ID,
            topic="candidate C1",
            actor="triager-autopilot",
            rationale="r",
        )
    finally:
        conn.close()

    rc = cmd_verifications(repo_root, ["list", "--json"], json_output=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] >= 1

    rc = cmd_verifications(repo_root, ["show", did, "--json"], json_output=True)
    assert rc == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["id"] == did


def test_verifications_show_finds_decision_outside_recent_window(repo_root: Path, capsys) -> None:
    """show must query by id, not search a truncated recent slice (#279 P2)."""
    from agentic_os.orchestrator import open_runtime

    conn, paths, events, _o = open_runtime(repo_root)
    try:
        Orchestrator(conn, paths, events).seed_phases()
        oldest = record_decision(
            conn, phase_id=CURRENT_PHASE_ID, topic="oldest", actor="triager-autopilot", rationale="r",
        )
        for i in range(60):
            record_decision(
                conn, phase_id=CURRENT_PHASE_ID, topic=f"d{i}", actor="triager-autopilot", rationale="r",
            )
    finally:
        conn.close()

    rc = cmd_verifications(repo_root, ["show", oldest, "--json"], json_output=True)
    assert rc == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["id"] == oldest
    assert shown["topic"] == "oldest"


def test_verifications_override_records_reversal(repo_root: Path, capsys) -> None:
    from agentic_os.orchestrator import open_runtime

    conn, paths, events, _o = open_runtime(repo_root)
    try:
        Orchestrator(conn, paths, events).seed_phases()
        did = record_decision(
            conn,
            phase_id=CURRENT_PHASE_ID,
            topic="t",
            actor="triager-autopilot",
            rationale="r",
        )
    finally:
        conn.close()

    rc = cmd_verifications(
        repo_root, ["override", did, "--severity", "S1", "--reason", "operator call"],
        json_output=True,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["reversed"] == did

    conn, _p, _e, _o = open_runtime(repo_root)
    try:
        row = conn.execute("SELECT reversed_by FROM decisions WHERE id=?;", (did,)).fetchone()
        assert row["reversed_by"] == payload["decision_id"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------


def _seed_invocation(conn, *, role, provider, session_id, tokens_in, tokens_out, cost):
    conn.execute(
        "INSERT INTO model_invocations(id, session_id, model_role, provider, command, "
        "started_at, tokens_in, tokens_out, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
        (
            "inv-" + role + session_id,
            session_id,
            role,
            provider,
            json.dumps(["x"]),
            now_iso(),
            tokens_in,
            tokens_out,
            cost,
        ),
    )


def test_budget_show_reports_usage_and_pct(repo_root: Path, capsys) -> None:
    from agentic_os.orchestrator import open_runtime

    conn, paths, events, _o = open_runtime(repo_root)
    try:
        _seed_invocation(conn, role="opus", provider="claude", session_id="S1",
                         tokens_in=100, tokens_out=150, cost=1.0)
    finally:
        conn.close()

    rc = cmd_budget(repo_root, ["show", "--session", "S1", "--json"], json_output=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session"]["tokens"] == 250
    assert payload["session"]["tokens_pct"] == 25.0  # 250/1000
    roles = {r["role"]: r for r in payload["per_role"]}
    assert roles["opus"]["tokens"] == 250
    assert roles["opus"]["pct"] == 50.0  # 250/500


def test_budget_set_persists_to_config(repo_root: Path, capsys) -> None:
    rc = cmd_budget(
        repo_root, ["set", "--role", "planner", "--max-tokens", "999", "--json"],
        json_output=True,
    )
    assert rc == 0
    from agentic_os.config import load_or_default

    cfg = load_or_default(repo_root)
    assert cfg.raw["budgets"]["per_role"]["planner"]["max_tokens"] == 999


def test_budget_reset_clears_session_invocations(repo_root: Path, capsys) -> None:
    from agentic_os.orchestrator import open_runtime

    conn, paths, events, _o = open_runtime(repo_root)
    try:
        _seed_invocation(conn, role="opus", provider="claude", session_id="S2",
                         tokens_in=10, tokens_out=10, cost=0.1)
    finally:
        conn.close()

    rc = cmd_budget(repo_root, ["reset", "--session", "S2", "--json"], json_output=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["deleted_invocations"] == 1


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------


def test_reports_list_show_diff(repo_root: Path, capsys) -> None:
    reports = repo_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "run-a.json").write_text(json.dumps({"passed": 8, "failed": 2}), encoding="utf-8")
    (reports / "run-b.json").write_text(json.dumps({"passed": 9, "failed": 1}), encoding="utf-8")

    rc = cmd_reports(repo_root, ["list", "--json"], json_output=True)
    assert rc == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["count"] >= 2

    rc = cmd_reports(repo_root, ["show", "run-a.json"], json_output=False)
    assert rc == 0
    assert "passed" in capsys.readouterr().out

    rc = cmd_reports(repo_root, ["diff", "run-a.json", "run-b.json", "--json"], json_output=True)
    assert rc == 0
    diff = json.loads(capsys.readouterr().out)
    assert diff["fields"]["passed"]["delta"] == 1
    assert diff["fields"]["failed"]["delta"] == -1


def test_reports_show_rejects_traversal(repo_root: Path) -> None:
    (repo_root / "reports").mkdir(parents=True, exist_ok=True)
    rc = cmd_reports(repo_root, ["show", "../config/agentic-os.yml"], json_output=False)
    assert rc == 4


# ---------------------------------------------------------------------------
# doctor --autonomy + bootstrap
# ---------------------------------------------------------------------------


def test_doctor_autonomy_exit_code_contract(repo_root: Path, capsys) -> None:
    rc = cmd_doctor(repo_root, ["--autonomy"], json_output=True)
    payload = json.loads(capsys.readouterr().out)
    autonomy = payload["autonomy"]
    assert autonomy["exit_code"] in (0, 2, 3, 4)
    assert rc == autonomy["exit_code"]
    assert set(autonomy["flags"]) == {
        "coverage_floor", "coverage_architect", "triage_batch", "exploratory_baseline",
    }


def test_bootstrap_fails_when_git_ensure_fails(repo_root: Path, capsys, monkeypatch) -> None:
    import agentic_os.sut_lifecycle as sl
    import agentic_os.sut_repo as sr

    monkeypatch.setattr(
        sl, "doctor_check_models",
        lambda models, smoke_timeout_seconds=20: {"issues": [], "roles": {}},
    )
    # Enable git so the bootstrap reaches the git_ensure gate.
    cfg_path = repo_root / "config" / "agentic-os.yml"
    cfg_path.write_text(cfg_path.read_text() + "\ngit:\n  enabled: true\n", encoding="utf-8")

    class _Report:
        ok = False
        summary = "remote unreachable"
        ops: list = []

    monkeypatch.setattr(sr, "git_ensure", lambda *a, **k: _Report())
    rc = cmd_autonomy(repo_root, ["bootstrap", "--no-start", "--json"], json_output=True)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 2, out
    steps = {s["step"]: s for s in payload["steps"]}
    assert steps["git_ensure"]["ok"] is False
    assert "start" not in steps  # gate stopped before start


def test_bootstrap_no_start_succeeds_with_healthy_providers(repo_root: Path, capsys, monkeypatch) -> None:
    # Stub provider smoke so the readiness gate is green without real CLIs.
    import agentic_os.sut_lifecycle as sl

    monkeypatch.setattr(
        sl, "doctor_check_models",
        lambda models, smoke_timeout_seconds=20: {"issues": [], "roles": {}},
    )
    rc = cmd_autonomy(repo_root, ["bootstrap", "--no-start", "--json"], json_output=True)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0, out
    assert payload["ok"] is True
    steps = {s["step"]: s for s in payload["steps"]}
    assert steps["init"]["ok"]
    assert steps["doctor"]["ok"]
    assert steps["start"].get("skipped") == "--no-start"


# ---------------------------------------------------------------------------
# new dashboard endpoints
# ---------------------------------------------------------------------------


def _get_json(host, port, path):
    with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


@pytest.fixture
def live(repo_root: Path):
    paths = RuntimePaths(repo_root=repo_root, runtime_root=repo_root / "agentic-os-runtime")
    paths.ensure()
    conn = init_db(paths.db)
    _seed_invocation(conn, role="opus", provider="claude", session_id="S9",
                     tokens_in=20, tokens_out=30, cost=0.5)
    from agentic_os.models.failover import mark_cooldown

    mark_cooldown(conn, role="planner", provider="claude", trigger="rate_limit",
                  cooldown_seconds=600)
    conn.close()

    httpd = make_server(paths, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    try:
        yield {"host": host, "port": port, "paths": paths}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_budget_status_endpoint(live) -> None:
    payload = _get_json(live["host"], live["port"], "/api/budget/status?session=S9")
    assert payload["session"]["tokens"] == 50
    assert payload["fail_mode"] == "warn"


def test_budget_status_endpoint_concurrent_polling_avoids_models_import(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths(repo_root=repo_root, runtime_root=repo_root / "agentic-os-runtime")
    paths.ensure()
    conn = init_db(paths.db)
    _seed_invocation(
        conn,
        role="opus",
        provider="claude",
        session_id="S10",
        tokens_in=10,
        tokens_out=5,
        cost=0.25,
    )
    conn.close()

    for name in list(sys.modules):
        if name == "agentic_os.models" or name.startswith("agentic_os.models."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    real_import = builtins.__import__

    def guard_models_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "agentic_os.models" or name.startswith("agentic_os.models."):
            raise AssertionError("budget status endpoint must not import agentic_os.models")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guard_models_import)
    httpd = make_server(paths, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            payloads = list(
                pool.map(
                    lambda _i: _get_json(host, port, "/api/budget/status?session=S10"),
                    range(16),
                )
            )
        assert all(p["session"]["tokens"] == 15 for p in payloads)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_provider_cooldowns_endpoint(live) -> None:
    payload = _get_json(live["host"], live["port"], "/api/providers/cooldowns")
    rows = payload["cooldowns"]
    assert any(r["provider"] == "claude" and r["role"] == "planner" for r in rows)
