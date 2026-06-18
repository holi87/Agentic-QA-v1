"""Runtime concurrency controls for parallel agent fan-out (issue #361).

This is the *substrate* the planner/implementer fan-out (#359/#360) consumes —
it does not parallelize the autonomy loop itself. It bounds in-flight agents
two ways and applies backpressure:

* **Global cap** — ``runtime.max_parallel_tasks`` (default 4). The absolute
  ceiling on concurrent agents across all roles; fan-out multiplies model
  calls, so this also protects provider rate limits.
* **Per-role cap** — optional ``runtime.max_parallel_per_role`` (e.g. one
  planner probe set, several implementer families). A role with no explicit
  cap inherits the global cap, so the global ceiling always binds.
* **Backpressure** — a role is refused a slot when its *entire* provider
  failover chain is on cooldown (all providers cold). One cold provider is
  not backpressure: the failover chain still has alive entries. The check is
  injected (``backpressure_check``) so this module stays free of config/DB
  coupling; the runtime wires it to ``models.failover.resolve_provider_chain``.

Acquisition order is always **role-semaphore then global-semaphore**. A thread
never holds the global slot while waiting on a role slot, so the wait graph has
no cycle — deadlock-free under arbitrary role mixes.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence


class Backpressured(RuntimeError):
    """Raised when a role's whole provider failover chain is on cooldown."""


class ConcurrencyController:
    def __init__(
        self,
        *,
        global_limit: int,
        per_role: Optional[Dict[str, int]] = None,
        backpressure_check: Optional[Callable[[str], bool]] = None,
        backpressure_poll_seconds: float = 0.05,
    ) -> None:
        if global_limit < 1:
            raise ValueError("global_limit must be >= 1")
        self._global_limit = int(global_limit)
        self._per_role_limits = {str(k): int(v) for k, v in (per_role or {}).items()}
        for role, limit in self._per_role_limits.items():
            if limit < 1:
                raise ValueError(f"per-role limit for {role!r} must be >= 1")
        self._backpressure_check = backpressure_check
        self._backpressure_poll_seconds = backpressure_poll_seconds

        self._global_sem = threading.BoundedSemaphore(self._global_limit)
        self._role_sems: Dict[str, threading.BoundedSemaphore] = {}
        self._counts: Dict[str, int] = {}
        self._state_lock = threading.Lock()

    # ---- limits ----------------------------------------------------------

    def role_limit(self, role: str) -> int:
        """Effective cap for a role — its explicit cap or the global cap."""
        return self._per_role_limits.get(role, self._global_limit)

    def _role_sem(self, role: str) -> threading.BoundedSemaphore:
        with self._state_lock:
            sem = self._role_sems.get(role)
            if sem is None:
                sem = threading.BoundedSemaphore(self.role_limit(role))
                self._role_sems[role] = sem
            return sem

    # ---- backpressure ----------------------------------------------------

    def is_backpressured(self, role: str) -> bool:
        return bool(self._backpressure_check and self._backpressure_check(role))

    def _await_backpressure_clear(self, role: str, block: bool, deadline: Optional[float]) -> None:
        if not self.is_backpressured(role):
            return
        if not block:
            raise Backpressured(f"role {role!r}: entire provider chain on cooldown")
        while self.is_backpressured(role):
            if deadline is not None and time.monotonic() >= deadline:
                raise Backpressured(f"role {role!r}: provider chain still cold after timeout")
            time.sleep(self._backpressure_poll_seconds)

    # ---- acquire / release ----------------------------------------------

    def _acquire_sems(self, role: str, block: bool, timeout: Optional[float]) -> bool:
        role_sem = self._role_sem(role)
        if not _sem_acquire(role_sem, block, timeout):
            return False
        if not _sem_acquire(self._global_sem, block, timeout):
            role_sem.release()
            return False
        with self._state_lock:
            self._counts[role] = self._counts.get(role, 0) + 1
        return True

    def acquire(self, role: str, *, block: bool = True, timeout: Optional[float] = None) -> None:
        """Take one slot for ``role``. Raises ``Backpressured`` if the role's
        provider chain is fully cold (and cannot clear within ``timeout``)."""
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        self._await_backpressure_clear(role, block, deadline)
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not self._acquire_sems(role, block, remaining):
            raise TimeoutError(f"could not acquire slot for role {role!r} within timeout")

    def try_acquire(self, role: str, *, timeout: Optional[float] = None) -> bool:
        """Non-raising acquire. Returns False on backpressure or cap saturation."""
        if self.is_backpressured(role):
            return False
        return self._acquire_sems(role, block=timeout is not None, timeout=timeout)

    def release(self, role: str) -> None:
        with self._state_lock:
            if self._counts.get(role, 0) <= 0:
                raise ValueError(f"release without matching acquire for role {role!r}")
            self._counts[role] -= 1
        self._global_sem.release()
        self._role_sem(role).release()

    @contextmanager
    def slot(self, role: str, *, timeout: Optional[float] = None) -> Iterator[None]:
        self.acquire(role, block=True, timeout=timeout)
        try:
            yield
        finally:
            self.release(role)

    # ---- observability ---------------------------------------------------

    def in_flight(self, role: Optional[str] = None) -> int:
        with self._state_lock:
            if role is None:
                return sum(self._counts.values())
            return self._counts.get(role, 0)


def _sem_acquire(sem: threading.BoundedSemaphore, block: bool, timeout: Optional[float]) -> bool:
    if not block:
        return sem.acquire(blocking=False)
    if timeout is None:
        return sem.acquire()
    return sem.acquire(timeout=timeout)


@dataclass(frozen=True)
class FanoutResult:
    """One slot's outcome in a :func:`fan_out` batch.

    Positionally aligned with the input thunks: ``results[i]`` is the outcome of
    ``thunks[i]``. ``ok`` is True only when the thunk ran to completion; on any
    failure (thunk raised, or the slot could not be acquired) ``ok`` is False,
    ``value`` is None and ``error`` holds the masked exception.
    """

    index: int
    ok: bool
    value: Any = None
    error: Optional[BaseException] = None


def fan_out(
    controller: ConcurrencyController,
    role: str,
    thunks: Sequence[Callable[[], Any]],
    *,
    timeout: Optional[float] = None,
) -> List[FanoutResult]:
    """Run ``thunks`` concurrently — one thread each under ``controller.slot``.

    The minimal fan-out primitive the planner (#359) and implementer (#360)
    consume. Guarantees:

    * **Order-preserving** — ``results[i]`` corresponds to ``thunks[i]``
      regardless of completion order.
    * **Bounded** — each thunk runs inside ``controller.slot(role)``, so peak
      concurrency never exceeds the role/global cap; excess threads block on
      acquisition rather than oversubscribing.
    * **Partial-failure tolerant / never raises** — a thunk that raises, or a
      slot that cannot be acquired (``Backpressured``/``TimeoutError`` when a
      ``timeout`` is set), is masked into a ``FanoutResult(ok=False)``. The
      caller (the join barrier) inspects results and decides what to do — e.g.
      record a gap for a failed probe — instead of one failure unwinding the
      whole batch.

    This intentionally carries **no** ordered-patch-merge or conflict semantics
    (that is #360's concern); it only runs work and reports per-item outcomes.
    """
    items = list(thunks)
    results: List[Optional[FanoutResult]] = [None] * len(items)

    def _run(index: int, thunk: Callable[[], Any]) -> None:
        # Wrap the WHOLE slot lifecycle: acquisition can raise (backpressure /
        # timeout) before the thunk ever runs, and that must be masked too.
        try:
            with controller.slot(role, timeout=timeout):
                value = thunk()
            results[index] = FanoutResult(index=index, ok=True, value=value)
        except BaseException as exc:  # noqa: BLE001 — mask EVERYTHING (the
            # contract is "never raises / report per item"). Catching only
            # Exception would let a BaseException (e.g. SystemExit) kill the
            # worker silently, dropping the slot from `results` so the join
            # barrier records NO gap — the failure would vanish unaudited.
            results[index] = FanoutResult(index=index, ok=False, error=exc)

    threads = [
        threading.Thread(
            target=_run, args=(i, thunk), name=f"fanout-{role}-{i}", daemon=True
        )
        for i, thunk in enumerate(items)
    ]
    # Issue #362 — guard `start()`. At a very high fan degree the OS can refuse
    # a new thread (`RuntimeError: can't start new thread`). An unguarded start
    # loop would propagate that and orphan the threads that DID start (their
    # results never joined, the failure unaudited). Mask a failed start into a
    # per-item FanoutResult — same contract as a failed thunk — and join only
    # the threads that actually started. Per-call work timeouts are bounded at
    # ACQUISITION via `controller.slot(role, timeout=timeout)` above.
    started: List[threading.Thread] = []
    for index, thread in enumerate(threads):
        try:
            thread.start()
        except (RuntimeError, OSError) as exc:
            results[index] = FanoutResult(index=index, ok=False, error=exc)
        else:
            started.append(thread)
    for thread in started:
        thread.join()
    # Every started worker masks its own failure and writes its index; a slot
    # whose start() failed is written above. So no None survives — the filter
    # is a defensive cast for the type checker.
    return [r for r in results if r is not None]


def build_concurrency_controller(
    config: Any,
    *,
    backpressure_check: Optional[Callable[[str], bool]] = None,
) -> ConcurrencyController:
    """Build the live controller from config.

    ``config`` may be an ``AgenticConfig`` (uses ``.raw``) or a raw mapping.
    The global cap is ``runtime.max_parallel_tasks`` (default 4); per-role caps
    come from the optional ``runtime.max_parallel_per_role`` mapping. The
    runtime supplies ``backpressure_check`` (see
    ``models.failover.all_providers_cold``); it is left None in contexts that
    do not gate on provider cooldown.
    """
    raw = getattr(config, "raw", config)
    runtime = raw.get("runtime", {}) if isinstance(raw, dict) else {}
    global_limit = int(runtime.get("max_parallel_tasks", 4) or 4)
    per_role_raw = runtime.get("max_parallel_per_role") or {}
    per_role = (
        {str(k): int(v) for k, v in per_role_raw.items()}
        if isinstance(per_role_raw, dict)
        else {}
    )
    return ConcurrencyController(
        global_limit=global_limit,
        per_role=per_role,
        backpressure_check=backpressure_check,
    )
