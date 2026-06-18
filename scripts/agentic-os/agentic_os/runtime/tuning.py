"""Central runtime tuning constants (issue #265).

Single home for the orchestrator's magic numbers so they are discoverable
and adjustable in one place. Values mirror the behaviour that previously
lived as module-level constants — this is a refactor, not a behaviour
change. In particular ``ACTIVE_POLL_SECONDS`` is 30 (the value the
autonomy loop already used), not the illustrative 5 some drafts mention.
"""
from __future__ import annotations

# autonomy.py — exploratory idle loop
EXPLORATORY_FAILURE_THRESHOLD = 3
PAUSED_POLL_SECONDS = 300
ACTIVE_POLL_SECONDS = 30

# events / session in-memory ring buffer
EVENTS_LOG_RING_SIZE = 500
RECORD_DETAIL_MAX_CHARS = 500

# work-item spec / HTTP body size guards.
# MAX_JSON_BODY_BYTES mirrors the existing dashboard_server limit (64 KiB);
# the wider 1 MiB figure in the issue draft would change behaviour, so the
# real runtime value wins.
MAX_SPEC_BYTES = 512 * 1024
MAX_JSON_BODY_BYTES = 64 * 1024

# graceful shutdown window for worker threads
SHUTDOWN_GRACE_SECONDS = 5

# learnings.py — cross-run learnings decay (issue #273).
# weight = exp(-age_days / decay_tau_days). NOTE: tau is an e-folding time,
# NOT a half-life — weight is 1/e ≈ 0.37 at age == tau. The half-life is
# tau * ln(2) ≈ 9.7 days for tau = 14. Rows decaying below
# LEARNING_MIN_WEIGHT are pruned by the nightly decay job.
LEARNING_DECAY_TAU_DAYS = 14.0
LEARNING_MIN_WEIGHT = 0.05
