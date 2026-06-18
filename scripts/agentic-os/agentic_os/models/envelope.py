"""Versioned JSON envelope for provider model outputs."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = "1.0"
_ENVELOPE_KEYS = {
    "schema_version",
    "provider",
    "provider_version",
    "role",
    "verdict",
    "reason",
    "citations",
    "body",
    "metadata",
}
_CITATION_KEYS = {"file", "line", "kind"}
_VALID_VERDICTS = {None, "APPROVE", "REJECT", "ABANDONED"}
_VALID_CITATION_KINDS = {"finding", "mitigation"}


class EnvelopeError(ValueError):
    """Raised when model output lacks a valid structured envelope."""


@dataclass(frozen=True)
class Citation:
    file: str
    line: int
    kind: str


@dataclass(frozen=True)
class ModelEnvelope:
    schema_version: str
    provider: str
    provider_version: str
    role: str
    verdict: Optional[str]
    reason: Optional[str]
    citations: List[Citation] = field(default_factory=list)
    body: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


def parse_model_envelope(
    stdout: str,
    *,
    provider: str,
    role: str,
    provider_version: str,
) -> ModelEnvelope:
    """Parse the last JSON-line envelope from provider stdout."""
    raw_envelope: Optional[dict[str, Any]] = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "envelope" in parsed:
            raw_envelope = parsed["envelope"]
    if raw_envelope is None:
        raise EnvelopeError("missing envelope JSON line")
    return model_envelope_from_dict(
        raw_envelope,
        expected_provider=provider,
        expected_role=role,
        expected_provider_version=provider_version,
    )


def model_envelope_from_dict(
    data: Any,
    *,
    expected_provider: str,
    expected_role: str,
    expected_provider_version: str,
) -> ModelEnvelope:
    if not isinstance(data, dict):
        raise EnvelopeError("envelope must be an object")
    extra = set(data) - _ENVELOPE_KEYS
    missing = _ENVELOPE_KEYS - set(data)
    if extra:
        raise EnvelopeError(f"envelope has unsupported fields: {sorted(extra)}")
    if missing:
        raise EnvelopeError(f"envelope missing fields: {sorted(missing)}")
    if data["schema_version"] != SCHEMA_VERSION:
        raise EnvelopeError(f"unsupported envelope schema: {data['schema_version']!r}")
    if data["provider"] != expected_provider:
        raise EnvelopeError("envelope provider does not match invocation")
    if data["role"] != expected_role:
        raise EnvelopeError("envelope role does not match invocation")
    if not isinstance(data["provider_version"], str) or not data["provider_version"]:
        raise EnvelopeError("provider_version must be a non-empty string")
    if expected_provider_version != "unknown" and data["provider_version"] != expected_provider_version:
        raise EnvelopeError("envelope provider_version does not match invocation")
    if data["verdict"] not in _VALID_VERDICTS:
        raise EnvelopeError(f"invalid envelope verdict: {data['verdict']!r}")
    if data["reason"] is not None and not isinstance(data["reason"], str):
        raise EnvelopeError("envelope reason must be string or null")
    if not isinstance(data["body"], str):
        raise EnvelopeError("envelope body must be a string")
    if not isinstance(data["metadata"], dict):
        raise EnvelopeError("envelope metadata must be an object")
    citations = _parse_citations(data["citations"])
    return ModelEnvelope(
        schema_version=data["schema_version"],
        provider=data["provider"],
        provider_version=data["provider_version"],
        role=data["role"],
        verdict=data["verdict"],
        reason=data["reason"],
        citations=citations,
        body=data["body"],
        metadata=dict(data["metadata"]),
    )


def _parse_citations(raw: Any) -> List[Citation]:
    if not isinstance(raw, list):
        raise EnvelopeError("envelope citations must be a list")
    citations: List[Citation] = []
    for item in raw:
        if not isinstance(item, dict):
            raise EnvelopeError("envelope citation must be an object")
        extra = set(item) - _CITATION_KEYS
        missing = _CITATION_KEYS - set(item)
        if extra:
            raise EnvelopeError(f"citation has unsupported fields: {sorted(extra)}")
        if missing:
            raise EnvelopeError(f"citation missing fields: {sorted(missing)}")
        if not isinstance(item["file"], str) or not item["file"]:
            raise EnvelopeError("citation.file must be a non-empty string")
        if not isinstance(item["line"], int) or item["line"] < 1:
            raise EnvelopeError("citation.line must be an integer >= 1")
        if item["kind"] not in _VALID_CITATION_KINDS:
            raise EnvelopeError(f"citation.kind invalid: {item['kind']!r}")
        citations.append(
            Citation(file=item["file"], line=item["line"], kind=item["kind"])
        )
    return citations


def envelope_json_line(envelope: ModelEnvelope) -> str:
    return json.dumps(
        {
            "envelope": {
                "schema_version": envelope.schema_version,
                "provider": envelope.provider,
                "provider_version": envelope.provider_version,
                "role": envelope.role,
                "verdict": envelope.verdict,
                "reason": envelope.reason,
                "citations": [
                    {"file": c.file, "line": c.line, "kind": c.kind}
                    for c in envelope.citations
                ],
                "body": envelope.body,
                "metadata": envelope.metadata,
            }
        },
        sort_keys=True,
    )
