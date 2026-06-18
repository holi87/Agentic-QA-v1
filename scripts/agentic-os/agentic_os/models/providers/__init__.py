"""Provider adapters for structured model envelopes."""
from __future__ import annotations

from ..envelope import ModelEnvelope


def parse_provider_stdout(
    provider: str,
    stdout: str,
    *,
    role: str,
    provider_version: str,
) -> ModelEnvelope:
    if provider == "claude":
        from .claude import parse
    elif provider == "codex":
        from .codex import parse
    elif provider == "antigravity":
        from .antigravity import parse
    else:
        from .script import parse
    return parse(stdout, role=role, provider_version=provider_version)
