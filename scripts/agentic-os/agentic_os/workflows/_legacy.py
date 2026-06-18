"""Re-export shim — workflow stages moved to stages/* (issue #292).

Kept so `from agentic_os.workflows._legacy import …` still resolves and the
workflows/__init__.py facade keeps working.
"""
from __future__ import annotations

from .stages._types import (  # noqa: F401
    WorkflowResult,
)
from .stages.attachments import (  # noqa: F401
    _attach_gate_to_work_item,
    _attach_run_artifacts_to_work_item,
    _display_command,
    _read_diff,
    _register_apply_artifact,
)
from .stages.dry_run import (  # noqa: F401
    MANIFEST_SCHEMA_VERSION,
    _REPORT_SOURCE_ARTIFACTS,
    _augment_manifest,
    _clean_report_source_artifacts,
    _env_hash,
    _run_fake_sut,
    _summarize_model_roles_for_manifest,
    _write_manifest,
    env_hash,
    run_dry_run,
    write_manifest,
)
from .stages.evidence import (  # noqa: F401
    _classify_evidence_kind,
    _persist_test_results_and_evidence,
    _scenario_tag,
    _sha256_safe,
    _triage_evidence,
)
from .stages.final_gate import (  # noqa: F401
    run_final_gate,
)
from .stages.finalize import (  # noqa: F401
    _zero_test_report_status,
    finalize_reports,
)
from .stages.idempotency import (  # noqa: F401
    _find_run_by_idempotency_key,
    _hash_file_if_available,
    _run_tests_idempotency_key,
    _work_item_test_inputs_fingerprint,
    _workflow_result_from_run_row,
)
from .stages.leases import (  # noqa: F401
    _acquire_reviewer_lease,
    _lease_expiry,
    _release_reviewer_lease,
    _review_gate_busy_result,
)
from .stages.recovery import (  # noqa: F401
    abandon_patch,
    run_recovery,
)
from .stages.review import (  # noqa: F401
    _SKILL_FAILURE_REJECT_THRESHOLD,
    _record_coverage_gap_from_review,
    _record_skill_failure_on_persistent_reject,
    _review_sut_key,
    run_review_gate,
)
from .stages.tests import (  # noqa: F401
    run_tests,
)
from .stages.triage import (  # noqa: F401
    _BUG_TAG_ID_RE,
    _detect_flaky_oscillation,
    _flaky_subject,
    _render_triage_markdown,
    _resolve_known_bug_tags,
    triage_reports,
)
