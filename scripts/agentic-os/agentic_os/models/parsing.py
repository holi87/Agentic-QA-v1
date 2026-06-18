"""Reviewer-output parsing, provider version probing, prompt redaction.

Split from models/__init__.py (issue #292).
"""
from __future__ import annotations

import re
import shutil
import subprocess as stdlib_subprocess
from typing import TYPE_CHECKING

from ..errors import UsageError
from ..gates import GateFinding, GateResult
from ..paths import RuntimePaths
from .envelope import EnvelopeError
from .providers import parse_provider_stdout

if TYPE_CHECKING:  # annotation-only; runtime import would create a cycle
    from .core import ModelInvocationResult


_SECRET_RE = re.compile(
    r"(?P<prefix>(?:bearer|token|api[_-]?key|secret|password)\s*[:=\s]\s*)(?P<value>[A-Za-z0-9_\-\.]{6,})",
    re.IGNORECASE,
)


def redact_prompt(text: str) -> str:
    """Scrub plain-looking secret literals from a prompt before persisting it."""
    def _replace(match: re.Match[str]) -> str:
        return match.group("prefix") + "<redacted>"

    return _SECRET_RE.sub(_replace, text)


def _provider_version(binary: str) -> str:
    resolved = shutil.which(binary)
    if not resolved:
        return "unknown"
    try:
        proc = stdlib_subprocess.run(
            [resolved, "--version"],
            stdout=stdlib_subprocess.PIPE,
            stderr=stdlib_subprocess.STDOUT,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return "unknown"
    first = (proc.stdout or "").strip().splitlines()
    return first[0][:120] if first else "unknown"


def parse_reviewer_invocation(result: ModelInvocationResult, paths: RuntimePaths) -> GateResult:
    """Parse a reviewer model output through the structured envelope."""
    if result.role != "reviewer":
        raise UsageError(f"parse_reviewer_invocation: role must be 'reviewer', got {result.role!r}")
    if result.output_path is None:
        raise UsageError("reviewer invocation has no output_path")
    text = (paths.repo_root / result.output_path).read_text(encoding="utf-8")
    try:
        envelope = parse_provider_stdout(
            result.provider,
            text,
            role=result.role,
            provider_version=result.provider_version,
        )
    except EnvelopeError as exc:
        return GateResult(
            verdict="REJECT",
            reason="envelope_invalid",
            findings=[GateFinding(result.output_path or "model-output", 1, str(exc))],
            raw_output=text,
        )
    verdict = envelope.verdict if envelope.verdict in {"APPROVE", "REJECT"} else "REJECT"
    reason = envelope.reason or ("missing_verdict" if envelope.verdict is None else "model_verdict")
    findings = [
        GateFinding(c.file, c.line, f"{c.kind}: {reason}")
        for c in envelope.citations
    ]
    return GateResult(verdict=verdict, reason=reason, findings=findings, raw_output=text)
