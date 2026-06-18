"""Shared model-output envelope prompt suffix."""
from __future__ import annotations


def envelope_prompt_suffix(role: str) -> str:
    return (
        "## Agentic OS model-output envelope\n\n"
        "End your response with exactly one JSON line of this shape, after any "
        "human-readable body:\n"
        "{\"envelope\":{\"schema_version\":\"1.0\",\"provider\":\"<provider>\","
        "\"provider_version\":\"<provider_version>\",\"role\":\""
        + role
        + "\",\"verdict\":\"APPROVE|REJECT|null\",\"reason\":\"<short reason or null>\","
        "\"citations\":[{\"file\":\"path\",\"line\":1,\"kind\":\"finding\"}],"
        "\"body\":\"<summary>\",\"metadata\":{\"tokens_in\":0,\"tokens_out\":0}}}\n"
        "Do not add keys outside this schema. Put non-verdict task output in "
        "`body` and set `verdict` to null when this role is not making a gate decision."
    )
