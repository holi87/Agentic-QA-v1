"""Antigravity CLI envelope adapter."""
from __future__ import annotations

from ..envelope import ModelEnvelope, parse_model_envelope


def parse(stdout: str, *, role: str, provider_version: str) -> ModelEnvelope:
    return parse_model_envelope(
        stdout,
        provider="antigravity",
        role=role,
        provider_version=provider_version,
    )
