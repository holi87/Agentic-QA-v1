"""Config package — split from agentic_os/config.py (issue #292).

Public surface re-exported from the submodules so existing
`from agentic_os.config import …` imports keep working.
"""
from __future__ import annotations

from ..errors import ConfigError  # noqa: F401  re-export for callers

from .loader import (  # noqa: F401
    _ACTIVE_OVERRIDE,
    get_active_config_override,
    load_config,
    load_or_default,
    resolve_config_path,
    set_active_config_override,
)
from .types import (  # noqa: F401
    AgenticConfig,
    DEFAULT_CONFIG_REL,
    LEGACY_CONFIG_REL,
    _ValidationCtx,
)
from .validators import (  # noqa: F401
    _API_RUNNERS,
    _AUTONOMY_ENUM_KEYS,
    _AUTONOMY_INT_KEYS,
    _CREDS_REF_TYPES,
    _GIT_BRANCH_RE,
    _MODEL_PROVIDERS,
    _MODEL_ROLES,
    _NOTIFICATION_KINDS,
    _OPTIONAL_AUTONOMY,
    _OPTIONAL_FALLBACK,
    _OPTIONAL_GIT,
    _OPTIONAL_MODEL,
    _OPTIONAL_SUT,
    _OPTIONAL_TOP,
    _QUEUE_POLICY_MODES,
    _REQUIRED_FALLBACK,
    _REQUIRED_GATES,
    _REQUIRED_HEALTHCHECK,
    _REQUIRED_MODEL,
    _REQUIRED_PATHS,
    _REQUIRED_REPORTS,
    _REQUIRED_RUNTIME,
    _REQUIRED_SUT,
    _REQUIRED_TIMEOUTS,
    _REQUIRED_TOP,
    _SOURCE_TYPES,
    _SUT_KINDS,
    _SUT_MODES,
    _TRANSCRIPT_CAPTURE_MODES,
    _UI_RUNNERS,
    _check_bool,
    _check_const,
    _check_int,
    _check_keys,
    _check_number,
    _check_safe_relpath,
    _check_signal_patterns,
    _check_source_list,
    _check_string,
    _check_url,
    _validate,
    _validate_autonomy,
    _validate_budgets,
    _validate_events,
    _validate_git,
    _validate_model_fallback,
    _validate_notifications,
    _validate_project,
    _validate_prompt_context,
    _validate_sut_v2,
)
from .writer import (  # noqa: F401
    redact_secrets,
    write_config,
)
