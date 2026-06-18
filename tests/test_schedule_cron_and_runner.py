"""Issue #271 — cron matcher, schedule store, runner, doctor, and CLI smoke."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_os.events import EventLog
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db
from agentic_os import scheduler as sched


# ---------------------------------------------------------------------------
# Cron matcher unit tests
# ---------------------------------------------------------------------------


def _dt(year=2026, month=5, day=27, hour=2, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_schedule_cron_wildcard_matches_every_minute() -> None:
    assert sched.cron_due("* * * * *", _dt(minute=0))
    assert sched.cron_due("* * * * *", _dt(hour=13, minute=37))


def test_schedule_cron_exact_minute_hour() -> None:
    assert sched.cron_due("0 2 * * *", _dt(hour=2, minute=0))
    assert not sched.cron_due("0 2 * * *", _dt(hour=2, minute=1))
    assert not sched.cron_due("0 2 * * *", _dt(hour=3, minute=0))


def test_schedule_cron_step_every_fifteen() -> None:
    assert sched.cron_due("*/15 * * * *", _dt(minute=0))
    assert sched.cron_due("*/15 * * * *", _dt(minute=15))
    assert sched.cron_due("*/15 * * * *", _dt(minute=45))
    assert not sched.cron_due("*/15 * * * *", _dt(minute=7))


def test_schedule_cron_list_and_range() -> None:
    assert sched.cron_due("1,2,5 * * * *", _dt(minute=2))
    assert not sched.cron_due("1,2,5 * * * *", _dt(minute=3))
    assert sched.cron_due("0 9-17 * * *", _dt(hour=9, minute=0))
    assert sched.cron_due("0 9-17 * * *", _dt(hour=17, minute=0))
    assert not sched.cron_due("0 9-17 * * *", _dt(hour=18, minute=0))


def test_schedule_cron_day_of_week_sunday_zero_and_seven() -> None:
    # 2026-05-31 is a Sunday.
    sunday = _dt(day=31, hour=0, minute=0)
    assert sunday.weekday() == 6
    assert sched.cron_due("0 0 * * 0", sunday)
    assert sched.cron_due("0 0 * * 7", sunday)
    monday = _dt(day=25, hour=0, minute=0)  # 2026-05-25 is a Monday
    assert not sched.cron_due("0 0 * * 0", monday)
    assert sched.cron_due("0 0 * * 1", monday)


def test_schedule_cron_dom_and_dow_use_and_semantics() -> None:
    # Documented AND semantics: both DOM and DOW must match when both set.
    # 2026-05-27 is a Wednesday (cron dow 3), day-of-month 27.
    wed27 = _dt(day=27, hour=0, minute=0)
    assert sched.cron_due("0 0 27 * 3", wed27)  # both match
    assert not sched.cron_due("0 0 27 * 1", wed27)  # DOM matches, DOW no
    assert not sched.cron_due("0 0 15 * 3", wed27)  # DOW matches, DOM no


def test_schedule_cron_invalid_expressions_raise() -> None:
    for bad in ("", "* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "a b c d e"):
        assert not sched.is_valid_cron(bad)
        with pytest.raises(sched.CronError):
            sched.parse_cron(bad)


def test_schedule_next_fire_returns_future_minute() -> None:
    now = _dt(hour=1, minute=30)
    nxt = sched.next_fire("0 2 * * *", now)
    assert nxt == _dt(hour=2, minute=0)
    assert nxt > now


# ---------------------------------------------------------------------------
# Store + runner fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runtime(tmp_path: Path):
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / "agentic-os-runtime")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    yield conn, events, paths
    conn.close()


def test_schedule_store_crud_roundtrip(runtime) -> None:
    conn, _events, _paths = runtime
    sched.add_schedule(conn, name="nightly", cron="0 2 * * *", action="autonomy start")
    rows = sched.list_schedules(conn)
    assert [r.name for r in rows] == ["nightly"]
    assert rows[0].enabled is True

    assert sched.set_enabled(conn, "nightly", False) is True
    assert sched.get_schedule(conn, "nightly").enabled is False

    # Upsert replaces cron/action.
    sched.add_schedule(conn, name="nightly", cron="*/5 * * * *", action="status")
    assert sched.get_schedule(conn, "nightly").cron == "*/5 * * * *"

    assert sched.remove_schedule(conn, "nightly") is True
    assert sched.get_schedule(conn, "nightly") is None
    assert sched.remove_schedule(conn, "nightly") is False


def test_schedule_add_rejects_invalid_cron(runtime) -> None:
    conn, _events, _paths = runtime
    with pytest.raises(sched.CronError):
        sched.add_schedule(conn, name="bad", cron="99 * * * *", action="status")


def test_schedule_runner_tick_fires_due_row_and_updates_last_run(runtime) -> None:
    conn, _events, paths = runtime
    sched.add_schedule(conn, name="tick", cron="* * * * *", action="status")

    fake_calls = []

    class _FakeProc:
        pid = 4242

    def _fake_popen(cmd, **kwargs):
        fake_calls.append(cmd)
        return _FakeProc()

    runner = sched.ScheduleRunner(paths, poll_seconds=0.01)
    now = _dt(hour=12, minute=0)
    # Patch Popen via the module-level default used by fire_schedule.
    import agentic_os.scheduler as mod

    orig = mod.subprocess.Popen
    mod.subprocess.Popen = _fake_popen  # type: ignore[assignment]
    try:
        fired = runner.tick(now=now)
    finally:
        mod.subprocess.Popen = orig  # type: ignore[assignment]

    assert len(fired) == 1
    assert fired[0]["status"] == "launched"
    assert fake_calls and fake_calls[0][:3] == [mod.sys.executable, "-m", "agentic_os"]

    refreshed = sched.get_schedule(conn, "tick")
    assert refreshed.last_run is not None
    assert refreshed.last_status == "launched"

    # Second tick in the SAME minute must NOT double-fire (minute dedupe).
    mod.subprocess.Popen = _fake_popen  # type: ignore[assignment]
    try:
        fired_again = runner.tick(now=now)
    finally:
        mod.subprocess.Popen = orig  # type: ignore[assignment]
    assert fired_again == []


def test_schedule_runner_tick_skips_disabled_and_non_due(runtime) -> None:
    conn, _events, paths = runtime
    sched.add_schedule(conn, name="off", cron="* * * * *", action="status", enabled=False)
    sched.add_schedule(conn, name="future", cron="0 2 * * *", action="status")
    runner = sched.ScheduleRunner(paths, poll_seconds=0.01)
    fired = runner.tick(now=_dt(hour=12, minute=30))
    assert fired == []


def test_schedule_runner_thread_starts_and_stops_cleanly(runtime) -> None:
    _conn, _events, paths = runtime
    runner = sched.ScheduleRunner(paths, poll_seconds=0.01)
    runner.start()
    assert runner._thread is not None and runner._thread.is_alive()
    runner.stop(timeout=2.0)
    assert runner._thread is None


def test_schedule_run_now_fires_regardless_of_cron(runtime) -> None:
    conn, events, paths = runtime
    sched.add_schedule(conn, name="adhoc", cron="0 2 * * *", action="status")
    import agentic_os.scheduler as mod

    class _FakeProc:
        pid = 7

    orig = mod.subprocess.Popen
    mod.subprocess.Popen = lambda cmd, **kw: _FakeProc()  # type: ignore[assignment]
    try:
        payload = sched.run_now(conn, events, paths, "adhoc", now=_dt(hour=12, minute=34))
    finally:
        mod.subprocess.Popen = orig  # type: ignore[assignment]
    assert payload["status"] == "launched"
    assert sched.get_schedule(conn, "adhoc").last_status == "launched"
    with pytest.raises(ValueError):
        sched.run_now(conn, events, paths, "missing")


def test_schedule_fired_event_written_to_ndjson(runtime) -> None:
    conn, events, paths = runtime
    sched.add_schedule(conn, name="evt", cron="* * * * *", action="status")
    import agentic_os.scheduler as mod

    class _FakeProc:
        pid = 1

    orig = mod.subprocess.Popen
    mod.subprocess.Popen = lambda cmd, **kw: _FakeProc()  # type: ignore[assignment]
    try:
        sched.run_now(conn, events, paths, "evt", now=_dt())
    finally:
        mod.subprocess.Popen = orig  # type: ignore[assignment]
    tail = events.tail(10)
    kinds = [e.get("kind") for e in tail]
    assert "schedule.fired" in kinds


# ---------------------------------------------------------------------------
# Doctor audit
# ---------------------------------------------------------------------------


def test_schedule_audit_flags_invalid_cron_as_issue(runtime) -> None:
    conn, _events, _paths = runtime
    # Insert an invalid cron directly (bypassing add_schedule validation).
    from agentic_os.storage.db import transaction

    with transaction(conn):
        conn.execute(
            "INSERT INTO schedules(name, cron, action, enabled) VALUES (?,?,?,1);",
            ("broken", "99 * * * *", "status"),
        )
    audit = sched.audit_schedules(sched.list_schedules(conn))
    assert audit["ok"] is False
    assert any("broken" in i for i in audit["issues"])


def test_schedule_audit_warns_on_stuck_schedule(runtime) -> None:
    conn, _events, _paths = runtime
    sched.add_schedule(conn, name="stuck", cron="*/5 * * * *", action="status")
    # last_run 1 hour ago; interval is 5min so 2x = 10min -> stuck.
    sched.record_run(conn, "stuck", status="launched", last_run="2026-05-27T11:00:00.000Z")
    now = _dt(hour=12, minute=0)
    audit = sched.audit_schedules(sched.list_schedules(conn), now=now)
    assert audit["ok"] is True  # warnings don't flip ok
    assert any("stuck" in w for w in audit["warnings"])


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    cfg = repo / "config" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "sut:\n  root: .\nmodels: {}\ndashboard:\n  enable_write_endpoints: false\n",
        encoding="utf-8",
    )
    return repo


def test_schedule_cli_add_list_remove(cli_repo: Path, capsys) -> None:
    from agentic_os.cli import cmd_schedule

    rc = cmd_schedule(
        cli_repo,
        ["add", "nightly", "--cron", "0 2 * * *", "--action", "autonomy start"],
        json_output=True,
    )
    assert rc == 0

    capsys.readouterr()  # drain the add output
    rc = cmd_schedule(cli_repo, ["list"], json_output=True)
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    names = [s["name"] for s in payload["schedules"]]
    assert "nightly" in names

    rc = cmd_schedule(cli_repo, ["disable", "nightly"], json_output=True)
    assert rc == 0
    rc = cmd_schedule(cli_repo, ["enable", "nightly"], json_output=True)
    assert rc == 0
    rc = cmd_schedule(cli_repo, ["remove", "nightly"], json_output=True)
    assert rc == 0


def test_schedule_cli_add_rejects_bad_cron(cli_repo: Path) -> None:
    from agentic_os.cli import cmd_schedule
    from agentic_os.errors import UsageError

    with pytest.raises(UsageError):
        cmd_schedule(
            cli_repo,
            ["add", "bad", "--cron", "99 * * * *", "--action", "status"],
            json_output=True,
        )


# ---------------------------------------------------------------------------
# Issue #271 review — config override survives the subprocess boundary (P1)
# ---------------------------------------------------------------------------


def test_build_action_command_threads_config_override(tmp_path: Path) -> None:
    cfg = tmp_path / "alt" / "agentic-os.yml"
    cmd = sched.build_action_command(
        tmp_path, "autonomy start --exploratory", config_override=cfg
    )
    # --config must precede the subcommand so the global parser consumes it.
    assert "--config" in cmd
    assert cmd[cmd.index("--config") + 1] == str(cfg)
    assert cmd.index("--config") < cmd.index("autonomy")


def test_build_action_command_without_override_has_no_config(tmp_path: Path) -> None:
    from agentic_os.config import set_active_config_override

    set_active_config_override(None)
    try:
        cmd = sched.build_action_command(tmp_path, "status")
        assert "--config" not in cmd
    finally:
        set_active_config_override(None)


def test_build_action_command_falls_back_to_active_override(tmp_path: Path) -> None:
    from agentic_os.config import set_active_config_override

    cfg = tmp_path / "active.yml"
    set_active_config_override(cfg)
    try:
        cmd = sched.build_action_command(tmp_path, "status")
        assert cmd[cmd.index("--config") + 1] == str(cfg)
    finally:
        set_active_config_override(None)
