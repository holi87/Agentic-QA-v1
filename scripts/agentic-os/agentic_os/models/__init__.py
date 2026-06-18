"""model invocation wrappers — package shim (split per issue #292).

Public surface re-exported from submodules so existing
`from agentic_os.models import …` imports keep working.

Submodules:
- core: invoke_model, _invoke_attempt, ModelInvocationResult, _AttemptResult
- budget: token estimation + budget pre-flight checks
- router: provider-chain ranking + swap-success recording
- parsing: reviewer-output parsing, provider version, prompt redaction
"""
from __future__ import annotations

from ..runtime.subprocess import run_command  # noqa: F401  monkey-patch surface
from .envelope import EnvelopeError  # noqa: F401  re-export
from .failover import (  # noqa: F401  re-export
    detect_failover_signal,
    mark_cooldown,
    resolve_provider_chain,
)
from .providers import parse_provider_stdout  # noqa: F401  re-export
from .providers.prompt_suffix import envelope_prompt_suffix  # noqa: F401  re-export

from .core import (  # noqa: F401
    ModelInvocationResult,
    _ALLOWED_ROLES,
    _AttemptResult,
    _PROVIDERS,
    _ROLE_TO_STEP_PHASE,
    _invoke_attempt,
    _is_under,
    invoke_model,
)
from .budget import (  # noqa: F401
    _check_budget_before_call,
    _cost_usd,
    _estimate_tokens,
    _provider_rates,
    _tokens_from_envelope,
)
from .router import (  # noqa: F401
    _rank_chain_by_quality,
    _record_swap_success,
)
from .parsing import (  # noqa: F401
    _SECRET_RE,
    _provider_version,
    parse_reviewer_invocation,
    redact_prompt,
)
