"""ISO-8601 UTC helpers (millisecond precision)."""
from __future__ import annotations

from datetime import datetime, timezone


def now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def today_iso_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
