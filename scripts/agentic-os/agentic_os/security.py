"""Path and command hardening helpers for local-only execution."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .errors import InfraError, UsageError


_SHELL_EXECUTABLES = {"sh", "bash", "zsh", "fish", "dash", "ksh"}
_SECRET_ENV_KEY_RE = re.compile(
    r"(api[_-]?key|apikey|secret|password|passwd|token|bearer|credential|"
    r"access[_-]?key|private[_-]?key|client[_-]?secret)",
    re.IGNORECASE,
)
_REDACTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("authorization", re.compile(r"Authorization:\s+\S+", re.IGNORECASE)),
    ("bearer", re.compile(r"\bbearer\s+\S+", re.IGNORECASE)),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{3,}")),
    ("github_token", re.compile(r"\bgh[ps]_[A-Za-z0-9_]{20,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    ("password", re.compile(r"\bpassword\s*[:=]\s*\S+", re.IGNORECASE)),
)


def resolve_repo_path(repo_root: Path, value: str, *, label: str, must_exist: bool = False) -> Path:
    """Resolve a config/user path and require it to stay under repo_root."""
    if "\x00" in value:
        raise UsageError(f"{label} contains a NUL byte")
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        raise UsageError(f"{label} must be relative to repo root: {value}")
    resolved = (repo_root / candidate).resolve()
    root = repo_root.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise UsageError(f"{label} escapes repo root: {value}") from exc
    if must_exist and not resolved.exists():
        raise InfraError(f"{label} does not exist: {value}")
    return resolved


def require_safe_argv(command: Sequence[str], *, allow_shell_wrapper: bool = False) -> list[str]:
    """Validate an argv command before it reaches subprocess execution."""
    if isinstance(command, str):
        raise UsageError("command must be an argv sequence, not a shell string")
    argv = list(command)
    if not argv:
        raise UsageError("command must not be empty")
    for part in argv:
        if not isinstance(part, str) or not part:
            raise UsageError("command must contain only non-empty strings")
        if "\x00" in part:
            raise UsageError("command argument contains a NUL byte")
    executable = Path(argv[0]).name
    if not allow_shell_wrapper and executable in _SHELL_EXECUTABLES and "-c" in argv[1:]:
        raise UsageError("shell -c commands are forbidden")
    return argv


def redaction_values_from_env(
    env: Mapping[str, str], *, extra_names: Iterable[str] = ()
) -> list[str]:
    """Return explicit env values that should never be written to logs.

    A value is collected when its variable NAME matches the secret-keyword
    heuristic OR when the name is explicitly declared in ``extra_names`` (issue
    #385 — the SUT config can declare a secret-bearing env var whose name has
    no keyword, e.g. ``DATABASE_URL`` via ``sut.db: {ref_type: env, value:
    DATABASE_URL}``).
    """
    declared = {str(name) for name in extra_names if name}
    values: list[str] = []
    for key, value in env.items():
        if not value or len(value) < 3:
            continue
        if _SECRET_ENV_KEY_RE.search(key) or key in declared:
            values.append(value)
    return values


def redact_sensitive_text(text: str, *, extra_values: Iterable[str] = ()) -> str:
    """Redact secret-shaped literals and explicit secret env values."""
    redacted = str(text)
    for value in sorted(set(extra_values), key=len, reverse=True):
        if value:
            redacted = redacted.replace(value, "[REDACTED:secret_in_env]")
    for rule, pattern in _REDACTION_PATTERNS:
        redacted = pattern.sub(f"[REDACTED:{rule}]", redacted)
    return redacted
