"""Atomic file writes for runtime state (issue #149).

State files (TEST-PLAN.json, manifests, leases, candidates.json) must
survive a crash mid-write so the next process start finds either the
old or new content — never a half-written JSON that crashes the parser.

`atomic_write_text` / `atomic_write_json` write to a unique sibling
``.tmp.<pid>.<seq>`` file, fsync the bytes, ``os.replace`` over the
target, then fsync the parent directory so the rename is durable on
POSIX. Parent-dir fsync is best-effort (some filesystems and Windows
reject it).

Concurrent writers used to collide on a fixed ``.tmp`` suffix — the
second ``os.replace`` would race against the first writer's cleanup and
raise ``FileNotFoundError`` (issue #161). The unique suffix removes the
rename race; for the higher-level last-write-wins problem on
read-modify-write callers (e.g. ``update_plan_candidate_decision``)
this module also exposes :func:`file_lock`, a cross-process /
cross-thread mutex.
"""
from __future__ import annotations

import contextlib
import itertools
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    import fcntl  # POSIX only; Windows callers fall back to thread-lock only.
except ImportError:  # pragma: no cover — non-POSIX fallback
    fcntl = None  # type: ignore[assignment]


_TMP_COUNTER = itertools.count()

# Process-wide thread mutexes keyed by absolute lock path. Two threads in
# the same process must serialize on the same key even before fcntl
# advisory locks kick in, because fcntl on Linux is per-process (a
# second thread in the same PID would not block on its own lock).
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write `content` to `path` atomically.

    Creates parent directories if missing. The bytes hit disk and the
    rename is durable before this function returns on POSIX.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{next(_TMP_COUNTER)}")
    data = content.encode(encoding)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        # os.write may return fewer bytes than requested (signal
        # interruption, resource pressure). Loop until the full payload
        # is on disk; otherwise the subsequent os.replace would publish
        # a truncated file as the new canonical state — the exact
        # corruption mode this helper is meant to prevent.
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError(f"os.write made no progress writing {tmp}")
            view = view[written:]
        try:
            os.fsync(fd)
        except OSError:
            # Some filesystems / pseudo FS reject fsync. Better to lose
            # the durability guarantee than crash the write.
            pass
    except Exception:
        # Leave no half-written tmp behind on failure.
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    else:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def atomic_write_json(
    path: Path,
    value: Any,
    *,
    indent: Optional[int] = 2,
    sort_keys: bool = True,
    ensure_ascii: bool = False,
    trailing_newline: bool = True,
) -> None:
    """Serialize `value` as JSON and write to `path` atomically."""
    payload = json.dumps(value, ensure_ascii=ensure_ascii, indent=indent, sort_keys=sort_keys)
    if trailing_newline and not payload.endswith("\n"):
        payload += "\n"
    atomic_write_text(path, payload)


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Exclusive cross-process / cross-thread lock keyed on ``path``.

    Creates a sibling ``<path>.lock`` file and acquires:

    1. A process-wide ``threading.Lock`` keyed by the lockfile path, so
       two threads inside the same interpreter serialize. ``fcntl.flock``
       on Linux is per-process and would not block a sibling thread that
       opened its own fd, so this layer is mandatory.
    2. A POSIX advisory ``fcntl.LOCK_EX``, so a second process (CLI
       running concurrently with the dashboard, for example) blocks
       until the first writer releases.

    On non-POSIX (Windows) systems ``fcntl`` is unavailable and only the
    thread-lock layer applies. That is acceptable here because the
    operator surfaces this guards — dashboard write endpoints and the
    CLI invoked from the same shell — run as a single process on
    Windows operator setups.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    key = str(lock_path.resolve()) if lock_path.exists() or path.exists() else str(lock_path)
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.get(key)
        if thread_lock is None:
            thread_lock = threading.Lock()
            _THREAD_LOCKS[key] = thread_lock

    with thread_lock:
        if fcntl is None:
            yield
            return
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
