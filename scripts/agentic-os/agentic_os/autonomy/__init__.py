"""autonomy/ package — split from autonomy.py (issue #292).

Re-exports every symbol via __init__.py so the existing
`from agentic_os.autonomy import …` imports keep working.

Orchestrator flow (per-phase dispatch helpers, see ``dispatch.py``):

* ``_autonomy_review_then_apply`` runs the review gate and, when the gate
  passes, applies the patch. It is the only path that may transition a work
  item to ``awaiting_operator_decision`` (loop.py).
* ``_autonomy_run_tests`` runs the post-apply test gate.
* ``_autonomy_final_gate`` runs the final reviewer gate.
"""
from __future__ import annotations

# Tests monkey-patch `autonomy.task_synthesis.synthesize_for_idle` — re-export
# the submodule so the legacy attribute access keeps working (issue #292).
from .. import task_synthesis  # noqa: F401

# Tuning constants tests monkey-patch on this package — bind them BEFORE
# importing submodules so the submodule-level _aut.X access proxies pick
# them up. Issue #292.
from ..runtime.tuning import (  # noqa: F401
    ACTIVE_POLL_SECONDS as _ACTIVE_POLL_SECONDS,
    EVENTS_LOG_RING_SIZE as _EVENTS_LOG_RING_SIZE,
    EXPLORATORY_FAILURE_THRESHOLD as _EXPLORATORY_FAILURE_THRESHOLD,
    PAUSED_POLL_SECONDS as _PAUSED_POLL_SECONDS,
    RECORD_DETAIL_MAX_CHARS as _RECORD_DETAIL_MAX_CHARS,
    SHUTDOWN_GRACE_SECONDS as _SHUTDOWN_GRACE_SECONDS,
)

from .dispatch import (  # noqa: F401
    _autonomy_final_gate,
    _autonomy_review_then_apply,
    _autonomy_run_tests,
)
from .exploratory import (  # noqa: F401
    _exploratory_enabled,
    _exploratory_pass,
    _latest_baseline_age_seconds,
    _maybe_exploratory_baseline,
    _online_endpoint_urls,
    _online_exploratory_pass,
    _online_web_url,
)
from .loop import (  # noqa: F401
    _FAILURE_STATUSES,
    _PHASE_ARTIFACT_KIND,
    _autonomy_step,
    _interpret_step_result,
    _phase_done,
    _record,
    _record_block_reason,
    _run_loop,
    _should_continue_to_review,
)
from .preflight import (  # noqa: F401
    preflight_check,
)
from .queue import (  # noqa: F401
    _PENDING_STATUSES,
    _load_cfg_best_effort,
    _resolve_active_project_best_effort,
    _resolve_queue_policy,
    _select_pending,
    _task_synthesis_cap,
    _task_synthesis_enabled,
    _wait_if_paused,
)
from .session_state import (  # noqa: F401
    AutonomyManager,
    _MANAGER,
    _SessionState,
    _now_iso,
    current_status,
    is_session_active,
    pause_session,
    resume_session,
    start_session,
    stop_session,
)
