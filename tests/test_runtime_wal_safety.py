"""Issue #361 — WAL safety + gate serialization under parallel agents.

Acceptance: N parallel agents run without DB corruption or double-approval.

These are the stress tests. They exercise the real deployment shape — each
worker owns its **own** SQLite connection to the shared WAL database (the
doctrine forbids sharing a writer; workers hand results to a serial barrier).
The invariants:

* **No corruption** — concurrent writers leave ``integrity_check == 'ok'`` and
  no foreign-key violations, and no write is lost.
* **No double-approval** — when N agents race to open the final gate for one
  work item, exactly one wins; the rest are refused (gates run once, serially).
* **Lease ownership** — only the token holder releases; an expired lease can be
  re-acquired by a new owner.
* **Event log is race-safe** — concurrent ``EventLog.write`` calls land every
  row in SQLite and emit one intact NDJSON line each (no torn lines).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from dataclasses import replace

from agentic_os.events import EventLog
from agentic_os.paths import runtime_paths
from agentic_os.storage.db import init_db, transaction
from agentic_os.workflows.stages.leases import (
    GateBusy,
    GateLease,
    acquire_gate_lease,
    release_gate_lease,
    serialized_gate,
)


def _seed_work_item(conn, wi_id: str = "wi-1") -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO work_items(id, title, status, spec_path, sut_root, priority,
                                   created_at, updated_at)
            VALUES (?, 'demo', 'reviewing', 'spec.md', '.', 'P1',
                    '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');
            """,
            (wi_id,),
        )


# ---- no double-approval --------------------------------------------------

def test_final_gate_exactly_one_winner(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths.ensure()
    conn = init_db(paths.db)
    try:
        _seed_work_item(conn)
    finally:
        conn.close()

    winners: list[int] = []
    winners_lock = threading.Lock()
    start = threading.Barrier(8)

    def worker(idx: int) -> None:
        # Single-shot acquisition, no release: models N agents racing to open
        # the final gate at the same instant. The winner holds the lease (it is
        # mid-merge); every loser must be refused, never queued behind it.
        c = init_db(paths.db)
        try:
            start.wait(timeout=5.0)
            lease = acquire_gate_lease(c, "final-gate", "wi-1")
            if lease is not None:
                with winners_lock:
                    winners.append(idx)
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert len(winners) == 1, f"double-approval: {winners}"


def test_autonomy_final_gate_refuses_when_lease_held(tmp_path: Path) -> None:
    """The live autonomy final-gate step is serialized: if another agent holds
    the final-gate lease for this work item, the step refuses (no second
    approval, no duplicate gate task)."""
    from agentic_os.autonomy.dispatch import _autonomy_final_gate

    paths = runtime_paths(tmp_path)
    paths.ensure()
    holder = init_db(paths.db)
    worker = init_db(paths.db)
    try:
        _seed_work_item(holder)
        assert acquire_gate_lease(holder, "final-gate", "wi-1") is not None
        events = EventLog(worker, paths)
        result = _autonomy_final_gate(worker, paths, events, work_item_id="wi-1")
        assert result["ok"] is False
        assert result["failure_kind"] == "infra"  # busy, not a product verdict
        # The refused attempt must not have created a final-gate task.
        tasks = worker.execute(
            "SELECT COUNT(*) FROM tasks WHERE kind='review';"
        ).fetchone()[0]
        assert tasks == 0
    finally:
        holder.close()
        worker.close()


def test_serialized_gate_refuses_when_held(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths.ensure()
    holder = init_db(paths.db)
    other = init_db(paths.db)
    try:
        _seed_work_item(holder)
        held = acquire_gate_lease(holder, "final-gate", "wi-1")
        assert held is not None
        # A second agent entering the gate while it is held is refused outright.
        with pytest.raises(GateBusy):
            with serialized_gate(other, "final-gate", "wi-1"):
                pass
        # And the held lease is untouched by the refused attempt.
        assert acquire_gate_lease(other, "final-gate", "wi-1") is None
    finally:
        holder.close()
        other.close()


def test_gate_lease_released_allows_reacquire(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths.ensure()
    conn = init_db(paths.db)
    try:
        _seed_work_item(conn)
        lease_a = acquire_gate_lease(conn, "final-gate", "wi-1")
        assert lease_a is not None
        # Held → a second acquire is refused.
        assert acquire_gate_lease(conn, "final-gate", "wi-1") is None
        assert release_gate_lease(conn, lease_a) is True
        # Released → re-acquire succeeds.
        assert acquire_gate_lease(conn, "final-gate", "wi-1") is not None
    finally:
        conn.close()


def test_gate_lease_release_is_fenced_against_stale_handle(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths.ensure()
    conn = init_db(paths.db)
    try:
        _seed_work_item(conn)
        lease_a = acquire_gate_lease(conn, "final-gate", "wi-1")
        assert lease_a is not None
        # A stale handle (e.g. an expired holder that was already superseded)
        # must NOT clear the live lease — the acquired_at fence rejects it.
        stale = replace(lease_a, acquired_at="1999-01-01T00:00:00.000Z")
        assert release_gate_lease(conn, stale) is False
        assert acquire_gate_lease(conn, "final-gate", "wi-1") is None  # still held
    finally:
        conn.close()


def test_expired_gate_lease_can_be_reacquired(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths.ensure()
    conn = init_db(paths.db)
    try:
        _seed_work_item(conn)
        assert acquire_gate_lease(conn, "final-gate", "wi-1", ttl_seconds=-1) is not None
        # ttl already elapsed → a fresh holder may take it.
        assert acquire_gate_lease(conn, "final-gate", "wi-1") is not None
    finally:
        conn.close()


# ---- no corruption / no lost writes --------------------------------------

def test_concurrent_writers_no_corruption_no_lost_writes(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths.ensure()
    n_threads, per_thread = 8, 40
    seed = init_db(paths.db)
    try:
        with transaction(seed):
            seed.execute("CREATE TABLE counter(id INTEGER PRIMARY KEY CHECK (id=1), n INTEGER NOT NULL);")
            seed.execute("INSERT INTO counter(id, n) VALUES (1, 0);")
    finally:
        seed.close()

    errors: list[BaseException] = []

    def worker() -> None:
        c = init_db(paths.db)
        try:
            for _ in range(per_thread):
                with transaction(c):
                    c.execute("UPDATE counter SET n = n + 1 WHERE id = 1;")
        except BaseException as exc:  # noqa: BLE001 — capture for assertion
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30.0)

    assert errors == [], f"writer errors: {errors!r}"
    check = init_db(paths.db)
    try:
        assert check.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
        assert check.execute("PRAGMA foreign_key_check;").fetchall() == []
        total = check.execute("SELECT n FROM counter WHERE id = 1;").fetchone()[0]
        assert total == n_threads * per_thread, "lost writes under contention"
    finally:
        check.close()


# ---- event log race safety -----------------------------------------------

def test_concurrent_event_writes_land_and_do_not_tear(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths.ensure()
    n_threads, per_thread = 6, 30

    def worker(idx: int) -> None:
        c = init_db(paths.db)
        try:
            events = EventLog(c, paths)
            for j in range(per_thread):
                events.write("test.tick", payload={"thread": idx, "seq": j})
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30.0)

    expected = n_threads * per_thread
    check = init_db(paths.db)
    try:
        rows = check.execute("SELECT COUNT(*) FROM events WHERE kind='test.tick';").fetchone()[0]
        assert rows == expected, "events lost in SQLite under concurrent writes"
    finally:
        check.close()

    # Every NDJSON line must be intact JSON (no torn/interleaved lines).
    ndjson_lines = 0
    for f in sorted(paths.events_dir.glob("*.ndjson")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            parsed = json.loads(line)  # raises on a torn line
            if parsed.get("kind") == "test.tick":
                ndjson_lines += 1
    assert ndjson_lines == expected, "events lost or torn in NDJSON under concurrent writes"


# ---- concurrent open safety (issue #362) ---------------------------------

def test_concurrent_connect_never_locks_on_open(tmp_path: Path) -> None:
    """N fan-out workers opening their own connection at the same instant must
    all succeed — the first flips the journal ``delete``→``wal`` (an exclusive
    lock) and SQLite hands the racing openers an immediate ``database is
    locked`` that ``busy_timeout`` does not retry. ``connect()`` retries the
    flip (#362); without it a worker crashes on open and is silently lost.

    The contended moment is the *first* open of a fresh DB, so the test runs
    many rounds, each against its own database, with a ``Barrier`` forcing all
    threads to hit ``connect()`` at once. One round catches the regression only
    probabilistically (~10–20%); compounding rounds drives the cumulative catch
    rate to ~100%, so a retry regression fails reliably in a single CI run
    rather than as a flake.
    """
    n_threads, rounds = 20, 30  # tuned: catches a retry regression ~100% per run
    errors: list[BaseException] = []

    for r in range(rounds):
        paths = runtime_paths(tmp_path / f"round_{r}")  # fresh DB → fresh flip race
        paths.ensure()
        gate = threading.Barrier(n_threads)

        def worker(idx: int, paths=paths, gate=gate) -> None:
            try:
                gate.wait(timeout=5.0)
                c = init_db(paths.db)  # connect() + migrate — the contended path
                try:
                    EventLog(c, paths).write("open.tick", payload={"w": idx})
                finally:
                    c.close()
            except BaseException as exc:  # noqa: BLE001 — capture for assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

        if errors:
            break  # fail fast — one crashed open is the whole point

        check = init_db(paths.db)
        try:
            rows = check.execute("SELECT COUNT(*) FROM events WHERE kind='open.tick';").fetchone()[0]
            assert rows == n_threads, f"round {r}: a worker was lost opening the DB under contention"
            assert check.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
        finally:
            check.close()

    assert errors == [], f"workers failed to open under contention: {errors!r}"
