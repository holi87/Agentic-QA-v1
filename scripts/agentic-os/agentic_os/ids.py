"""ULID generation using stdlib only (Crockford Base32, monotonic per process)."""
from __future__ import annotations

import os
import threading
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_lock = threading.Lock()
_last_ms = -1
_last_rand = 0


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def ulid() -> str:
    """Return a 26-char ULID. Monotonic within the same millisecond."""
    global _last_ms, _last_rand
    with _lock:
        ms = int(time.time() * 1000)
        if ms == _last_ms:
            _last_rand += 1
            rand = _last_rand
        else:
            _last_ms = ms
            rand = int.from_bytes(os.urandom(10), "big")
            _last_rand = rand
        time_part = _encode(ms, 10)
        rand_part = _encode(rand & ((1 << 80) - 1), 16)
        return time_part + rand_part


def run_id() -> str:
    return f"run-{ulid()}"
