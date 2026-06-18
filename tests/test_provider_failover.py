"""Provider failover signal detection, cooldown registry, and fallback resolution."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_os.models.failover import (
    FailoverSignal,
    active_cooldowns,
    clear_cooldown,
    detect_failover_signal,
    is_cold,
    mark_cooldown,
    resolve_provider_chain,
)
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db


def _conn(tmp_path: Path):
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    return init_db(paths.db)


# ---- signal detection -------------------------------------------------------


def test_detect_failover_signal_matches_rate_limit_in_stderr() -> None:
    signal = detect_failover_signal(
        stdout="",
        stderr="ERROR: rate limit exceeded",
        exit_code=1,
    )
    assert signal.matched is True
    assert "rate" in signal.trigger or "rate.?limit" in signal.trigger


def test_detect_failover_signal_matches_anthropic_json_overloaded() -> None:
    raw = '{"error":{"type":"overloaded_error","message":"try later"}}'
    signal = detect_failover_signal(stdout=raw, stderr="", exit_code=1)
    assert signal.matched is True
    assert "overloaded_error" in signal.trigger


def test_detect_failover_signal_ignores_clean_failure() -> None:
    signal = detect_failover_signal(
        stdout="all good",
        stderr="exit 1",
        exit_code=1,
    )
    assert signal.matched is False


def test_detect_failover_signal_extra_signals_pick_up_custom_pattern() -> None:
    signal = detect_failover_signal(
        stdout="our custom marker LIMIT_REACHED",
        stderr="",
        exit_code=0,
        extra_signals=("LIMIT_REACHED",),
    )
    assert signal.matched is True
    assert "LIMIT_REACHED" in signal.trigger


# ---- cooldown registry ------------------------------------------------------


def test_mark_cooldown_then_is_cold(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    expires = mark_cooldown(conn, role="planner", provider="claude", trigger="rate_limit", cooldown_seconds=60)
    assert expires.endswith("Z")
    assert is_cold(conn, role="planner", provider="claude") is True
    # Different role shares no state.
    assert is_cold(conn, role="implementer", provider="claude") is False


def test_active_cooldowns_filters_expired_rows(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    mark_cooldown(conn, role="planner", provider="claude", trigger="rate_limit", cooldown_seconds=60)
    # Inject an already-expired row directly to confirm filtering.
    past = (datetime.now(tz=timezone.utc) - timedelta(seconds=10)).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn.execute(
        "INSERT INTO provider_cooldowns(role, provider, cooldown_until, trigger, updated_at) VALUES (?, ?, ?, ?, ?);",
        ("reviewer", "codex", past, "expired", past),
    )
    rows = active_cooldowns(conn)
    providers = {(r["role"], r["provider"]) for r in rows}
    assert ("planner", "claude") in providers
    assert ("reviewer", "codex") not in providers


def test_clear_cooldown_removes_row(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    mark_cooldown(conn, role="planner", provider="claude", trigger="rate_limit", cooldown_seconds=60)
    clear_cooldown(conn, role="planner", provider="claude")
    assert is_cold(conn, role="planner", provider="claude") is False


# ---- chain resolution -------------------------------------------------------


def test_resolve_provider_chain_skips_cold_entries(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    mark_cooldown(conn, role="planner", provider="claude", trigger="rate_limit", cooldown_seconds=60)
    primary = {"provider": "claude", "command": ["claude"], "role": "opus"}
    fallback = [
        {"provider": "codex", "command": ["codex"], "role": "codex"},
        {"provider": "antigravity", "command": ["agy"], "role": "gemini"},
    ]
    chain = resolve_provider_chain(primary=primary, fallback=fallback, conn=conn, role="planner")
    providers = [c["provider"] for c in chain]
    assert providers == ["codex", "antigravity"]


def test_invoke_model_falls_back_to_next_provider_on_rate_limit(tmp_path: Path) -> None:
    """End-to-end: a primary CLI emitting a rate_limit signal forces fallback."""
    import os
    from agentic_os.events import EventLog
    from agentic_os.models import invoke_model
    from agentic_os.models.failover import is_cold

    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    def _write_script(path: Path, *, version: str, body_stdout: str, body_stderr: str = "", exit_code: int = 0) -> None:
        path.write_text(
            "#!/usr/bin/env bash\n"
            f"if [ \"${{1:-}}\" = \"--version\" ]; then echo '{version}'; exit 0; fi\n"
            "cat >/dev/null\n"
            f"printf '%s' {body_stdout!r}\n"
            + (f"printf '%s' {body_stderr!r} 1>&2\n" if body_stderr else "")
            + f"exit {exit_code}\n",
            encoding="utf-8",
        )
        os.chmod(path, 0o755)

    primary = bin_dir / "fake-primary"
    fallback = bin_dir / "fake-fallback"
    envelope_template = (
        '{{"envelope":{{"schema_version":"1.0","provider":"script",'
        '"provider_version":"{ver}","role":"planner","verdict":null,'
        '"reason":null,"citations":[],"body":"ok","metadata":{{}}}}}}\n'
    )
    # Primary returns clean stdout but a rate-limit message in stderr.
    _write_script(
        primary,
        version="fake-primary 1.0",
        body_stdout=envelope_template.format(ver="fake-primary 1.0"),
        body_stderr="ERROR: 429 rate limit exceeded\n",
        exit_code=1,
    )
    _write_script(
        fallback,
        version="fake-fallback 1.0",
        body_stdout=envelope_template.format(ver="fake-fallback 1.0"),
        exit_code=0,
    )

    config = {
        "models": {
            "planner": {
                "provider": "script",
                "command": [str(primary)],
                "role": "script",
                "fallback": [
                    {
                        "provider": "claude",
                        "command": [str(fallback)],
                        "role": "sonnet",
                    }
                ],
            }
        }
    }
    result = invoke_model(
        conn,
        paths,
        events,
        role="planner",
        config=config,
        prompt="plan this",
        timeout_seconds=5,
    )
    assert result.provider == "claude"
    assert result.exit_code == 0
    kinds = [e["kind"] for e in events.tail(40)]
    assert "provider_failover" in kinds
    # The primary provider must now be cold for this role.
    assert is_cold(conn, role="planner", provider="script") is True


def test_resolve_provider_chain_keeps_primary_when_all_cold(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    for provider in ("claude", "codex", "antigravity"):
        mark_cooldown(conn, role="planner", provider=provider, trigger="rate_limit", cooldown_seconds=60)
    primary = {"provider": "claude", "command": ["claude"], "role": "opus"}
    fallback = [
        {"provider": "codex", "command": ["codex"], "role": "codex"},
        {"provider": "antigravity", "command": ["agy"], "role": "gemini"},
    ]
    chain = resolve_provider_chain(primary=primary, fallback=fallback, conn=conn, role="planner")
    assert len(chain) == 1
    assert chain[0]["provider"] == "claude"
