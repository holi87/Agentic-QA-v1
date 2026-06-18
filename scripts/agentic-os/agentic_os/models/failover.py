"""Issue #235 — provider failover detection + cooldown bookkeeping.

The runtime calls `detect_failover_signal` against a CommandResult (or the
captured stdout/stderr text) to decide whether the current provider should
be swapped for the next entry in the chain. Cooldown state lives in
SQLite (`provider_cooldowns`) so the orchestrator survives restart.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Sequence

from ..time_utils import now_iso


# Default trigger regexes. Operators can extend via
# `models.<role>.fallback_signals` (per-role) or
# `models.<role>.fallback[N].fallback_signals` (per-entry).
_DEFAULT_SIGNALS: tuple[re.Pattern[str], ...] = (
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"\bquota\b", re.IGNORECASE),
    re.compile(r"\b429\b"),
    re.compile(r"\b(401|403)\b.*token", re.IGNORECASE),
    re.compile(r"auth.*expired", re.IGNORECASE),
    re.compile(r"usage.*limit", re.IGNORECASE),
    re.compile(r"model.*overloaded", re.IGNORECASE),
)

_DEFAULT_COOLDOWN_SECONDS = 600


@dataclass(frozen=True)
class FailoverSignal:
    matched: bool
    trigger: str
    detail: str


def detect_failover_signal(
    *,
    stdout: str,
    stderr: str,
    exit_code: int,
    extra_signals: Sequence[str] = (),
) -> FailoverSignal:
    """Return a matched failover signal when any of the trigger heuristics fire."""
    extra_patterns = tuple(re.compile(p, re.IGNORECASE) for p in extra_signals if p)
    patterns: tuple[re.Pattern[str], ...] = _DEFAULT_SIGNALS + extra_patterns
    for stream_name, text in (("stderr", stderr), ("stdout", stdout)):
        if not text:
            continue
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                return FailoverSignal(
                    matched=True,
                    trigger=f"regex:{pattern.pattern}",
                    detail=f"{stream_name}:{match.group(0)[:80]}",
                )
        # Anthropic SDK structured JSON shape.
        json_signal = _detect_anthropic_json(text)
        if json_signal is not None:
            return json_signal
    if exit_code != 0 and exit_code != 1:
        # Conservative — only exit code 2+ (infra) maps to failover by default.
        # The issue calls out {1,2} but only when paired with stderr matching,
        # so a bare non-zero exit on its own does NOT trigger failover.
        pass
    return FailoverSignal(matched=False, trigger="", detail="")


def _detect_anthropic_json(text: str) -> Optional[FailoverSignal]:
    """Detect `{"error":{"type":"rate_limit_error"|"overloaded_error"|...}}` shapes."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{") or "error" not in stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        err = obj.get("error") if isinstance(obj, dict) else None
        if not isinstance(err, dict):
            continue
        err_type = err.get("type")
        if isinstance(err_type, str) and err_type in {
            "rate_limit_error",
            "overloaded_error",
            "authentication_error",
            "permission_error",
        }:
            return FailoverSignal(
                matched=True,
                trigger=f"sdk_json:{err_type}",
                detail=f"json:{err_type}",
            )
    return None


# ---------------------------------------------------------------------------
# Cooldown registry
# ---------------------------------------------------------------------------


def mark_cooldown(
    conn: sqlite3.Connection,
    *,
    role: str,
    provider: str,
    trigger: str,
    cooldown_seconds: int = _DEFAULT_COOLDOWN_SECONDS,
) -> str:
    """Mark a provider cold; returns the cooldown_until ISO timestamp."""
    expires = _now_utc() + timedelta(seconds=max(0, cooldown_seconds))
    expires_iso = expires.isoformat(timespec="seconds").replace("+00:00", "Z")
    conn.execute(
        """
        INSERT INTO provider_cooldowns(role, provider, cooldown_until, trigger, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(role, provider) DO UPDATE SET
          cooldown_until=excluded.cooldown_until,
          trigger=excluded.trigger,
          updated_at=excluded.updated_at;
        """,
        (role, provider, expires_iso, trigger, now_iso()),
    )
    return expires_iso


def is_cold(conn: sqlite3.Connection, *, role: str, provider: str) -> bool:
    """True when the provider is still inside its cooldown window for this role."""
    row = conn.execute(
        "SELECT cooldown_until FROM provider_cooldowns WHERE role=? AND provider=?;",
        (role, provider),
    ).fetchone()
    if row is None:
        return False
    return _parse_iso(row["cooldown_until"] if hasattr(row, "keys") else row[0]) > _now_utc()


def active_cooldowns(conn: sqlite3.Connection, *, role: Optional[str] = None) -> List[dict]:
    """Return rows whose cooldown_until is still in the future."""
    now = _now_utc().isoformat(timespec="seconds").replace("+00:00", "Z")
    if role is None:
        rows = conn.execute(
            "SELECT role, provider, cooldown_until, trigger, updated_at "
            "FROM provider_cooldowns WHERE cooldown_until > ?;",
            (now,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT role, provider, cooldown_until, trigger, updated_at "
            "FROM provider_cooldowns WHERE role=? AND cooldown_until > ?;",
            (role, now),
        ).fetchall()
    return [dict(zip(("role", "provider", "cooldown_until", "trigger", "updated_at"), row)) for row in rows]


def clear_cooldown(conn: sqlite3.Connection, *, role: str, provider: str) -> None:
    """Drop a cooldown row (manual reset path for operators)."""
    conn.execute(
        "DELETE FROM provider_cooldowns WHERE role=? AND provider=?;",
        (role, provider),
    )


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_iso(value: str) -> datetime:
    raw = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        # Fallback: treat unparseable rows as expired so they no longer block.
        return _now_utc() - timedelta(seconds=1)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# Chain resolution
# ---------------------------------------------------------------------------


def resolve_provider_chain(
    *,
    primary: dict,
    fallback: Iterable[dict],
    conn: sqlite3.Connection,
    role: str,
) -> List[dict]:
    """Return the ordered list of provider configs to try, skipping cold ones.

    Primary first, then fallback entries in declaration order. Cold entries
    are filtered out. If everything is cold, the original primary is kept
    as the last-resort attempt so we still surface a real failure rather
    than silently dropping the call.
    """
    candidates: list[dict] = [primary] + [c for c in fallback if isinstance(c, dict)]
    alive = [c for c in candidates if not is_cold(conn, role=role, provider=str(c.get("provider", "")))]
    if not alive:
        return candidates[:1]
    return alive


def all_providers_cold(
    conn: sqlite3.Connection,
    *,
    primary: dict,
    fallback: Iterable[dict],
    role: str,
) -> bool:
    """Backpressure signal (issue #361): True iff EVERY provider in the role's
    failover chain is on cooldown.

    Note: this checks coldness directly rather than ``resolve_provider_chain``,
    which deliberately keeps the primary as a last-resort attempt when all are
    cold (so a call still surfaces a real error). Backpressure is the opposite
    question — should we hold off dispatching at all — so an all-cold chain
    must read as backpressured.
    """
    candidates: list[dict] = [primary] + [c for c in fallback if isinstance(c, dict)]
    providers = [str(c.get("provider", "")) for c in candidates if c.get("provider")]
    if not providers:
        return False
    return all(is_cold(conn, role=role, provider=p) for p in providers)
