"""Safe subprocess runner for Agentic OS.

All external commands must pass through this module: commands are argv lists,
never shell strings; stdout and stderr are captured into one run log; timeout
handling terminates the whole process group before returning an infra exit.
"""
from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess as stdlib_subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from ..security import redact_sensitive_text, redaction_values_from_env, require_safe_argv
from ..time_utils import now_iso

INFRA_EXIT_CODE = 2
# Core process env every child needs: locate binaries, resolve $HOME, keep
# locale/timezone stable, and honour the runtime temp dir.
_CORE_INHERITED_ENV = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
)
# Provider credentials — model CLIs need these to authenticate. Operators
# export them in the parent shell; without forwarding, every model call
# fails auth. Codex PR #275 review (P1) flagged the drop.
#
# Issue #291 — these are forwarded ONLY when `include_provider_credentials`
# is true. Untrusted SUT commands (healthcheck, test_runner) launch with
# `include_provider_credentials=False` so a hostile SUT binary cannot read
# the operator's model keys out of its environment.
_PROVIDER_CREDENTIAL_ENV = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
)
# Back-compat alias: the full allowlist used by model invocations.
_ALLOWED_INHERITED_ENV = _CORE_INHERITED_ENV + _PROVIDER_CREDENTIAL_ENV


def scrub_provider_credentials(env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of `env` with operator model credentials removed.

    Issue #291 — callers that pass an explicit `env` (e.g. a full
    `os.environ` copy for a SUT test_runner) override the allowlist, so the
    `include_provider_credentials=False` flag alone cannot keep model keys
    out. Scrub the explicit env through this helper first.
    """
    return {k: v for k, v in env.items() if k not in _PROVIDER_CREDENTIAL_ENV}
_CURATED_PATH_DIRS = (
    Path("/usr/bin"),
    Path("/usr/local/bin"),
    Path("/opt/homebrew/bin"),
)


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    cwd: str
    exit_code: int
    raw_exit_code: Optional[int]
    duration_ms: int
    log_path: Path
    started_at: str
    finished_at: str
    failure_kind: Optional[str]
    timed_out: bool


def run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    log_path: Path,
    timeout_seconds: int,
    shutdown_grace_seconds: int = 5,
    env: Optional[Mapping[str, str]] = None,
    input_text: Optional[str] = None,
    include_provider_credentials: bool = True,
    secret_env_names: Sequence[str] = (),
) -> CommandResult:
    argv = _validate_command(command)
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if shutdown_grace_seconds <= 0:
        raise ValueError("shutdown_grace_seconds must be positive")

    cwd = cwd.resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = now_iso()
    monotonic_start = time.monotonic()
    timed_out = False
    raw_exit_code: Optional[int] = None
    exit_code = INFRA_EXIT_CODE
    child_env = _build_child_env(
        env, include_provider_credentials=include_provider_credentials
    )
    redaction_values = redaction_values_from_env(child_env, extra_names=secret_env_names)

    with log_path.open("w", encoding="utf-8") as log:
        _write_status(
            log,
            "started",
            {
                "command": argv,
                "cwd": str(cwd),
                "timeout_seconds": timeout_seconds,
            },
            redaction_values=redaction_values,
        )
        try:
            # Issue #102 — wire stdin so callers can stream a prompt
            # to a model CLI without depending on argv placeholders.
            proc = stdlib_subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=child_env,
                stdin=stdlib_subprocess.PIPE if input_text is not None else None,
                stdout=stdlib_subprocess.PIPE,
                stderr=stdlib_subprocess.PIPE,
                text=True,
                bufsize=1,
                shell=False,
                start_new_session=True,
            )
            if input_text is not None and proc.stdin is not None:
                try:
                    proc.stdin.write(input_text)
                    proc.stdin.flush()
                finally:
                    proc.stdin.close()
        except OSError as exc:
            finished_at = now_iso()
            duration_ms = _duration_ms(monotonic_start)
            _write_status(
                log,
                "spawn_failed",
                {
                    "error": str(exc),
                    "exit_code": INFRA_EXIT_CODE,
                    "duration_ms": duration_ms,
                    "finished_at": finished_at,
                },
                redaction_values=redaction_values,
            )
            return CommandResult(
                command=argv,
                cwd=str(cwd),
                exit_code=INFRA_EXIT_CODE,
                raw_exit_code=None,
                duration_ms=duration_ms,
                log_path=log_path,
                started_at=started_at,
                finished_at=finished_at,
                failure_kind="infra",
                timed_out=False,
            )

        threads = [
            threading.Thread(
                target=_pump,
                args=(proc.stdout, "stdout", log, redaction_values),
                daemon=True,
            ),
            threading.Thread(
                target=_pump,
                args=(proc.stderr, "stderr", log, redaction_values),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()

        try:
            raw_exit_code = proc.wait(timeout=timeout_seconds)
        except stdlib_subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_group(proc, shutdown_grace_seconds)
            raw_exit_code = proc.returncode
            exit_code = INFRA_EXIT_CODE
        else:
            exit_code = int(raw_exit_code)
        finally:
            for thread in threads:
                thread.join(timeout=1)

        finished_at = now_iso()
        duration_ms = _duration_ms(monotonic_start)
        failure_kind = _failure_kind(exit_code, timed_out)
        _write_status(
            log,
            "finished",
            {
                "exit_code": exit_code,
                "raw_exit_code": raw_exit_code,
                "duration_ms": duration_ms,
                "finished_at": finished_at,
                "failure_kind": failure_kind,
                "timed_out": timed_out,
            },
            redaction_values=redaction_values,
        )

    return CommandResult(
        command=argv,
        cwd=str(cwd),
        exit_code=exit_code,
        raw_exit_code=raw_exit_code,
        duration_ms=duration_ms,
        log_path=log_path,
        started_at=started_at,
        finished_at=finished_at,
        failure_kind=failure_kind,
        timed_out=timed_out,
    )


def _validate_command(command: Sequence[str]) -> list[str]:
    argv = require_safe_argv(command, allow_shell_wrapper=False)
    argv[0] = _resolve_executable(argv[0])
    return argv


def _resolve_executable(executable: str) -> str:
    path = Path(executable)
    if path.is_absolute():
        return str(path)
    for root in _CURATED_PATH_DIRS:
        candidate = root / executable
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    found = shutil.which(executable)
    hint = f" (found outside curated PATH at {found})" if found else ""
    raise ValueError(
        "command executable must be absolute or resolvable from curated PATH "
        f"{[str(p) for p in _CURATED_PATH_DIRS]}: {executable!r}{hint}"
    )


def _build_child_env(
    extra: Optional[Mapping[str, str]] = None,
    *,
    include_provider_credentials: bool = True,
) -> dict[str, str]:
    allowed = _CORE_INHERITED_ENV
    if include_provider_credentials:
        allowed = allowed + _PROVIDER_CREDENTIAL_ENV
    base = {key: os.environ[key] for key in allowed if key in os.environ}
    if extra:
        base.update({str(key): str(value) for key, value in extra.items()})
    return base


def _pump(stream: object, label: str, log, redaction_values: Sequence[str]) -> None:
    if stream is None:
        return
    for line in stream:
        safe_line = redact_sensitive_text(line, extra_values=redaction_values)
        log.write(f"[{label}] {safe_line}")
        log.flush()


def _terminate_process_group(proc: stdlib_subprocess.Popen[str], grace_seconds: int) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        proc.terminate()
    try:
        proc.wait(timeout=grace_seconds)
    except stdlib_subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            proc.kill()
        proc.wait(timeout=grace_seconds)


def _write_status(
    log,
    status: str,
    payload: dict[str, object],
    *,
    redaction_values: Sequence[str],
) -> None:
    line = json.dumps({"status": status, **payload}, sort_keys=True)
    log.write("[status] " + redact_sensitive_text(line, extra_values=redaction_values) + "\n")
    log.flush()


def _duration_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _failure_kind(exit_code: int, timed_out: bool) -> Optional[str]:
    if timed_out:
        return "timeout"
    if exit_code == 0:
        return None
    if exit_code == 1:
        return "product"
    if exit_code == INFRA_EXIT_CODE:
        return "infra"
    return "unknown"
