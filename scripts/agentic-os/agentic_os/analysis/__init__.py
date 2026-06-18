"""Work-item analysis — package shim (split from analysis.py per issue #292).

Public surface re-exported from submodules so existing
`from agentic_os.analysis import …` imports keep working.
"""
from __future__ import annotations

from .types import _AnalysisInputs  # noqa: F401
from .extractors import (  # noqa: F401
    _API_HINT,
    _API_METHOD_PATH,
    _API_METHOD_URL,
    _ROUTE_HINT,
    _SECTION_RE,
    _UI_HINT,
    _default_cleanup_for_method,
    _derive_api_expected_assertion,
    _extract_api_mentions,
    _extract_ui_routes,
    _first_ui_route,
    _has_ui_intent,
    _is_negative_or_boundary,
    _priority_for_text,
    _spec_sections,
    _surface_enabled,
    _without_urls,
)
from .inputs import (  # noqa: F401
    _MAX_URL_FETCH_BYTES,
    _NoPrivateRedirectHandler,
    _collect_inputs,
    _safe_fetch_url,
    _validate_url_host_not_private,
)
from .sut_map import _build_sut_map  # noqa: F401
from .builders import (  # noqa: F401
    CANDIDATE_BUCKETS,
    _A11Y_HINT,
    _SECURITY_HINT,
    _build_candidate_tests,
    _build_requirements,
    _build_risk_map,
)
from .coverage_architect import (  # noqa: F401
    _AUTONOMOUS_SAFE_API_METHODS,
    _GAP_BUCKET_TO_CATEGORY,
    _UI_FORM_HINTS,
    _apply_coverage_architect,
    _autopilot_decision_rule,
    _coverage_architect_enabled,
    _record_recurring_coverage_gaps,
    _sut_key,
)
from .orchestrator import (  # noqa: F401
    ANALYSIS_KIND_BY_FILENAME,
    _attach_analyzer_note,
    analyze_work_item,
)
