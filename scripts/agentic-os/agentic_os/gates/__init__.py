"""Gate evaluation — package shim (split from gates.py per issue #292).

Public surface re-exported from submodules so existing
`from agentic_os.gates import …` imports keep working.
"""
from __future__ import annotations

from .types import (  # noqa: F401
    GateFinding,
    GateResult,
    PatchMergeResult,
)
from .parse import (  # noqa: F401
    _FINDING_RE,
    _value_line,
    parse_gate_output,
)
from .static_review import (  # noqa: F401
    _ASSERTION_PATTERNS,
    _BUG_TAG_RE,
    _KNOWN_BUG_RE,
    _OPERATOR_DECISION_RE,
    _RAW_GENERATOR_INTERPOLATION_RE,
    _SKIP_PATTERNS,
    _UNTRUSTED_PROMPT_SOURCE_RE,
    _has_known_bug_pair,
    _has_operator_decision_marker,
    _is_assertion_line,
    _is_generator_path,
    _is_test_path,
    _parse_hunk_lines,
    _reason_for,
    _scan_added_line,
    _scan_removed_line,
    static_review_gate,
)
from .pillars import (  # noqa: F401
    FINAL_GATE_REQUIRED_FILES,
    _load_last_run,
    _pillar_patch_resolution,
    _pillar_required_files,
    _tag_pillar,
)
from .violations import (  # noqa: F401
    _BUG_FILE_RE,
    find_bug_evidence_violations,
    find_known_bug_policy_violations,
    find_run_report_violations,
    find_work_item_run_attestation_violations,
)
from .final import (  # noqa: F401
    _PILLAR_CHECKS,
    evaluate_final_gate,
    final_gate,
)
from .patch_gate import (  # noqa: F401
    RESOLVED_VERDICTS,
    _has_apply_artifact_after_patch,
    _has_approved_gate_after_patch,
    _read_resolution_verdict,
    _resolve_patch_state,
    describe_blocking_patches,
    find_patch_gate_violations,
    merge_patch_if_approved,
)
from .io import (  # noqa: F401
    _artifact_path,
    _read_gate_binding,
    write_abandon_artifact,
    write_gate_result,
)
