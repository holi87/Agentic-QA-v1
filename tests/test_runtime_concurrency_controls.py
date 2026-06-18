"""Issue #361 — runtime concurrency controls (global + per-role caps, backpressure).

The OS fans out independent runtime work (planner probes, implementer
test-families, triage). ``ConcurrencyController`` is the substrate #359/#360
consume: it bounds in-flight agents globally and per role, and refuses new
slots for a role whose entire provider failover chain is on cooldown
(backpressure). It does NOT itself parallelize the autonomy loop.
"""
from __future__ import annotations

import threading
import time

import pytest

from agentic_os.autonomy.concurrency import (
    Backpressured,
    ConcurrencyController,
    fan_out,
)


def _peak_under_contention(ctrl: ConcurrencyController, role: str, n_threads: int) -> int:
    """Spawn n_threads contending for `role` slots; return the peak observed
    concurrency. Each thread holds its slot until `release` is set, so the peak
    equals the enforced cap when threads outnumber slots."""
    active = 0
    peak = 0
    lock = threading.Lock()
    release = threading.Event()
    ready = threading.Semaphore(0)

    def worker() -> None:
        nonlocal active, peak
        with ctrl.slot(role):
            with lock:
                active += 1
                peak = max(peak, active)
            ready.release()
            release.wait(timeout=5.0)
            with lock:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    # Wait until as many slots as the cap allows are occupied, then read peak.
    time.sleep(0.3)
    release.set()
    for t in threads:
        t.join(timeout=5.0)
    return peak


def test_global_cap_never_exceeded() -> None:
    ctrl = ConcurrencyController(global_limit=2)
    peak = _peak_under_contention(ctrl, "implementer", n_threads=6)
    assert peak == 2  # six threads, two slots → exactly two ever active


def test_per_role_cap_independent_of_global() -> None:
    ctrl = ConcurrencyController(global_limit=8, per_role={"planner": 1})
    peak = _peak_under_contention(ctrl, "planner", n_threads=5)
    assert peak == 1  # per-role cap of 1 binds before the generous global cap


def test_role_without_explicit_cap_uses_global() -> None:
    ctrl = ConcurrencyController(global_limit=3, per_role={"planner": 1})
    peak = _peak_under_contention(ctrl, "triage", n_threads=6)
    assert peak == 3  # unconfigured role falls back to the global cap


def test_slot_releases_on_exception() -> None:
    ctrl = ConcurrencyController(global_limit=1)
    with pytest.raises(RuntimeError):
        with ctrl.slot("implementer"):
            raise RuntimeError("boom")
    # Slot must be free again — a second acquisition must not block forever.
    acquired = ctrl.try_acquire("implementer", timeout=1.0)
    assert acquired is True
    ctrl.release("implementer")


def test_in_flight_accounting() -> None:
    ctrl = ConcurrencyController(global_limit=4)
    assert ctrl.in_flight() == 0
    with ctrl.slot("planner"):
        assert ctrl.in_flight() == 1
        assert ctrl.in_flight("planner") == 1
        assert ctrl.in_flight("implementer") == 0
    assert ctrl.in_flight() == 0


def test_backpressure_blocks_role_when_chain_cold() -> None:
    cold = {"planner"}
    ctrl = ConcurrencyController(
        global_limit=4,
        backpressure_check=lambda role: role in cold,
    )
    # A backpressured role refuses a non-blocking acquire.
    with pytest.raises(Backpressured):
        ctrl.acquire("planner", block=False)
    # Other roles are unaffected.
    with ctrl.slot("implementer"):
        assert ctrl.in_flight("implementer") == 1


def test_backpressure_clears_when_chain_recovers() -> None:
    cold = {"planner"}
    ctrl = ConcurrencyController(
        global_limit=4,
        backpressure_check=lambda role: role in cold,
    )
    with pytest.raises(Backpressured):
        ctrl.acquire("planner", block=False)
    cold.discard("planner")  # provider chain recovered
    with ctrl.slot("planner"):
        assert ctrl.in_flight("planner") == 1


# ---- fan_out (issue #359) ---------------------------------------------------
# `fan_out` is the minimal order-preserving, partial-failure-tolerant fan-out
# the planner (#359) and implementer (#360) consume. It runs one thread per
# thunk under `controller.slot(role)`, NEVER raises (every failure — thunk
# exception OR slot-acquisition refusal — is masked into a FanoutResult), and
# returns results positionally aligned with the input thunks.


def _overlap_thunk(active: list, peak: list, lock: threading.Lock, gate: threading.Event):
    """A thunk that holds its slot ~briefly while recording peak concurrency."""

    def _thunk():
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        # Hold the slot long enough for siblings to overlap under the cap.
        gate.wait(timeout=2.0)
        with lock:
            active[0] -= 1
        return "ok"

    return _thunk


def test_fan_out_preserves_order_and_returns_all() -> None:
    ctrl = ConcurrencyController(global_limit=4)
    # Reverse-finishing thunks: thunk i sleeps (N-i)*small so completion order
    # is the reverse of submission order — results must still be in input order.
    thunks = [(lambda i=i: (time.sleep((4 - i) * 0.02), i * 10)[1]) for i in range(4)]
    results = fan_out(ctrl, "planner", thunks)
    assert [r.index for r in results] == [0, 1, 2, 3]
    assert [r.ok for r in results] == [True, True, True, True]
    assert [r.value for r in results] == [0, 10, 20, 30]


def test_fan_out_runs_thunks_concurrently() -> None:
    ctrl = ConcurrencyController(global_limit=3)
    active, peak, lock, gate = [0], [0], threading.Lock(), threading.Event()
    thunks = [_overlap_thunk(active, peak, lock, gate) for _ in range(3)]

    done = threading.Event()

    def _runner():
        fan_out(ctrl, "planner", thunks)
        done.set()

    runner = threading.Thread(target=_runner)
    runner.start()
    time.sleep(0.3)  # let all three occupy slots
    gate.set()
    runner.join(timeout=5.0)
    assert done.is_set()
    assert peak[0] == 3  # all three overlapped under the generous cap


def test_fan_out_never_exceeds_cap() -> None:
    ctrl = ConcurrencyController(global_limit=2)
    active, peak, lock, gate = [0], [0], threading.Lock(), threading.Event()
    thunks = [_overlap_thunk(active, peak, lock, gate) for _ in range(6)]

    done = threading.Event()

    def _runner():
        fan_out(ctrl, "planner", thunks)
        done.set()

    runner = threading.Thread(target=_runner)
    runner.start()
    time.sleep(0.3)
    gate.set()
    runner.join(timeout=5.0)
    assert done.is_set()
    assert peak[0] == 2  # six thunks, two slots → never more than two at once


def test_fan_out_masks_thunk_exception() -> None:
    ctrl = ConcurrencyController(global_limit=4)

    def _boom():
        raise ValueError("probe failed")

    thunks = [lambda: "a", _boom, lambda: "c"]
    results = fan_out(ctrl, "planner", thunks)  # must NOT raise
    assert results[0].ok is True and results[0].value == "a"
    assert results[1].ok is False and results[1].value is None
    assert isinstance(results[1].error, ValueError)
    assert results[2].ok is True and results[2].value == "c"
    # The controller is left clean — failed thunk still released its slot.
    assert ctrl.in_flight("planner") == 0


def test_fan_out_masks_acquire_backpressure() -> None:
    # Every probe's role is fully cold: slot() raises Backpressured on ACQUIRE,
    # before the thunk ever runs. fan_out must mask that too — not propagate.
    ran = [False]

    def _thunk():
        ran[0] = True
        return "should-not-run"

    ctrl = ConcurrencyController(
        global_limit=4,
        backpressure_check=lambda role: True,
    )
    # timeout bounds the acquire wait; permanently-cold chain → Backpressured.
    results = fan_out(ctrl, "planner", [_thunk, _thunk], timeout=0.3)
    assert ran[0] is False
    assert all(r.ok is False for r in results)
    assert all(isinstance(r.error, Backpressured) for r in results)
    assert ctrl.in_flight("planner") == 0


def test_fan_out_masks_base_exception_thunk() -> None:
    # A thunk raising a *BaseException* (not Exception) must still be masked
    # into a FanoutResult — never silently drop its slot. A dropped slot leaves
    # no result for the join barrier to inspect, so the caller records NO gap
    # and the failure becomes invisible (audit-defeating).
    ctrl = ConcurrencyController(global_limit=4)

    class _Fatal(BaseException):
        pass

    def _fatal():
        raise _Fatal("fatal")

    results = fan_out(ctrl, "planner", [lambda: "a", _fatal, lambda: "c"])
    assert [r.index for r in results] == [0, 1, 2]  # slot 1 NOT dropped
    assert results[0].ok is True and results[0].value == "a"
    assert results[1].ok is False and isinstance(results[1].error, _Fatal)
    assert results[2].ok is True and results[2].value == "c"
    assert ctrl.in_flight("planner") == 0  # masked failure still released


def test_fan_out_empty_thunks() -> None:
    ctrl = ConcurrencyController(global_limit=4)
    assert fan_out(ctrl, "planner", []) == []


def test_fan_out_masks_failed_thread_start(monkeypatch) -> None:
    """Issue #362 — at a very high fan degree the OS can refuse a new thread
    (`RuntimeError: can't start new thread`). fan_out must mask a failed
    start() into a per-item FanoutResult and still join + return the workers
    that DID start — never propagate the error or orphan started threads."""
    ctrl = ConcurrencyController(global_limit=4)
    real_start = threading.Thread.start

    def flaky_start(self) -> None:
        if self.name.endswith("-1"):  # the 2nd work-unit's thread refuses to start
            raise RuntimeError("can't start new thread")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", flaky_start)
    results = fan_out(ctrl, "planner", [lambda: "a", lambda: "b", lambda: "c"])

    # Order preserved and slot 1 present (a dropped slot would leave NO gap).
    assert [r.index for r in results] == [0, 1, 2]
    assert results[0].ok is True and results[0].value == "a"
    assert results[1].ok is False and isinstance(results[1].error, RuntimeError)
    assert results[2].ok is True and results[2].value == "c"
    # Started workers released their slots; the failed-start slot took none.
    assert ctrl.in_flight("planner") == 0
