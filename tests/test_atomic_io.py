"""Tests for atomic state writes (issue #149) + cross-writer locks (issue #161)."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from agentic_os.atomic_io import atomic_write_json, atomic_write_text, file_lock


def test_atomic_write_text_creates_file(tmp_path: Path):
    target = tmp_path / "out.txt"
    atomic_write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_atomic_write_text_overwrites_existing(tmp_path: Path):
    target = tmp_path / "out.txt"
    target.write_text("old content")
    atomic_write_text(target, "new content")
    assert target.read_text(encoding="utf-8") == "new content"


def test_atomic_write_text_creates_parent_dirs(tmp_path: Path):
    target = tmp_path / "deep" / "nested" / "file.txt"
    atomic_write_text(target, "ok")
    assert target.read_text(encoding="utf-8") == "ok"


def test_atomic_write_text_no_tmp_left_behind(tmp_path: Path):
    target = tmp_path / "out.txt"
    atomic_write_text(target, "ok")
    siblings = list(tmp_path.iterdir())
    assert siblings == [target]


def test_atomic_write_json_round_trip(tmp_path: Path):
    target = tmp_path / "data.json"
    payload = {"b": 1, "a": [1, 2, 3], "nested": {"y": True}}
    atomic_write_json(target, payload)
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded == payload


def test_atomic_write_json_sorts_keys_by_default(tmp_path: Path):
    target = tmp_path / "data.json"
    atomic_write_json(target, {"b": 1, "a": 2})
    text = target.read_text(encoding="utf-8")
    assert text.index('"a"') < text.index('"b"')


def test_atomic_write_json_trailing_newline_default(tmp_path: Path):
    target = tmp_path / "data.json"
    atomic_write_json(target, {"x": 1})
    assert target.read_text(encoding="utf-8").endswith("\n")


def test_atomic_write_json_trailing_newline_opt_out(tmp_path: Path):
    target = tmp_path / "data.json"
    atomic_write_json(target, {"x": 1}, trailing_newline=False)
    assert not target.read_text(encoding="utf-8").endswith("\n")


def test_atomic_write_json_unicode_default(tmp_path: Path):
    target = tmp_path / "data.json"
    atomic_write_json(target, {"msg": "zażółć"})
    text = target.read_text(encoding="utf-8")
    assert "zażółć" in text


def test_atomic_write_json_ensure_ascii(tmp_path: Path):
    target = tmp_path / "data.json"
    atomic_write_json(target, {"msg": "zażółć"}, ensure_ascii=True)
    text = target.read_text(encoding="utf-8")
    assert "\\u017c" in text  # ż escaped


def test_atomic_write_text_replace_failure_leaves_target_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "out.txt"
    target.write_text("original")

    real_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(target, "should not appear")

    # Original content preserved — half-written tmp never visible at target.
    assert target.read_text(encoding="utf-8") == "original"


def test_atomic_write_text_handles_short_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # os.write may return fewer bytes than requested. The helper must
    # loop until everything is on disk, otherwise os.replace would
    # publish a truncated file (Codex review on PR #151).
    target = tmp_path / "out.json"
    payload = '{"big": "' + ("x" * 4096) + '"}'

    real_write = os.write
    call_count = {"n": 0}

    def chunky_write(fd, data):
        call_count["n"] += 1
        # First call writes only 7 bytes, then full writes.
        if call_count["n"] == 1:
            return real_write(fd, bytes(data[:7]))
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", chunky_write)
    atomic_write_text(target, payload)
    assert target.read_text(encoding="utf-8") == payload
    assert call_count["n"] >= 2


def test_atomic_write_text_zero_progress_raises_and_cleans_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "out.txt"

    def stuck_write(fd, data):
        return 0

    monkeypatch.setattr(os, "write", stuck_write)
    with pytest.raises(OSError, match="no progress"):
        atomic_write_text(target, "anything")
    assert not target.exists()
    # No unique tmp left behind on failure (issue #161 suffix scheme).
    leftover = [p.name for p in tmp_path.iterdir() if p.name.startswith("out.txt.tmp")]
    assert leftover == [], leftover


def test_atomic_write_json_handles_existing_tmp_sibling(tmp_path: Path):
    # Issue #161 — tmp suffix is now unique per writer (.tmp.<pid>.<seq>)
    # so a stale legacy ".tmp" from a previous crash does not collide.
    # The write must succeed and the target must hold the new content;
    # legacy litter remains untouched (caller's responsibility to sweep).
    target = tmp_path / "data.json"
    legacy_tmp = tmp_path / "data.json.tmp"
    legacy_tmp.write_text("stale junk")
    atomic_write_json(target, {"x": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"x": 1}
    # Our own unique tmp is gone (renamed onto target):
    leftover_unique = [
        p.name for p in tmp_path.iterdir()
        if p.name.startswith("data.json.tmp.")
    ]
    assert leftover_unique == [], leftover_unique
    # Legacy stale ".tmp" survives — harmless, no collision.
    assert legacy_tmp.exists()


def test_concurrent_atomic_writes_do_not_lose_a_writer(tmp_path: Path):
    """Issue #161 — two threads writing the same target via
    ``atomic_write_text`` must both land an ``os.replace`` without
    raising ``FileNotFoundError`` on a shared ``.tmp`` suffix.

    Without the unique-suffix change a second writer's rename used to
    race the first writer's tmp file and either crash or publish a
    truncated state. We can't deterministically assert *which* writer
    wins (last-write-wins is acceptable at this layer — :func:`file_lock`
    is what callers use to serialize read-modify-write); we only assert
    no exception propagated and the final content is one of the inputs.
    """
    target = tmp_path / "shared.txt"
    errors: list[BaseException] = []

    def writer(content: str):
        try:
            for _ in range(20):
                atomic_write_text(target, content)
        except BaseException as exc:  # noqa: BLE001 — collect for assert
            errors.append(exc)

    t1 = threading.Thread(target=writer, args=("from-a",))
    t2 = threading.Thread(target=writer, args=("from-b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert not errors, errors
    assert target.read_text(encoding="utf-8") in {"from-a", "from-b"}
    # No tmp litter from either thread.
    leftover = [p.name for p in tmp_path.iterdir() if p.name.startswith("shared.txt.tmp")]
    assert leftover == [], leftover


def test_file_lock_serializes_concurrent_critical_sections(tmp_path: Path):
    """Issue #161 — :func:`file_lock` is a mutex. Two threads that
    enter the critical section must not overlap; we assert this by
    timestamping enter / exit and checking the intervals are disjoint.
    """
    target = tmp_path / "guarded.json"
    enter: list[float] = []
    exit_: list[float] = []
    barrier = threading.Barrier(2)

    def critical_section():
        barrier.wait()
        with file_lock(target):
            enter.append(time.monotonic())
            time.sleep(0.05)
            exit_.append(time.monotonic())

    threads = [threading.Thread(target=critical_section) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(enter) == 2 and len(exit_) == 2
    # Sort intervals by enter timestamp; the second enter must follow
    # the first exit (no overlap).
    ordered = sorted(zip(enter, exit_))
    assert ordered[1][0] >= ordered[0][1], ordered
