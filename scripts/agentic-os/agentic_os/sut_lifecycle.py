"""SUT lifecycle: Docker Compose, healthcheck, doctor.

This module owns starting, healthchecking, and stopping the SUT according to
the operator's config. Every command is argv-only (never a shell string).
Failures coming from the platform (missing docker, missing compose file) are
infra failures with exit code 2. Failures from the SUT itself (healthcheck
times out, application crashed) are also infra failures from the perspective
of Agentic OS — the product cannot be tested if it never becomes healthy.
"""
from __future__ import annotations

import shutil
import subprocess as stdlib_subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .errors import InfraError, UsageError
from .events import EventLog
from .paths import RuntimePaths
from .runtime.subprocess import run_command
from .time_utils import now_iso


INFRA_EXIT_CODE = 2


@dataclass(frozen=True)
class SutLifecycleResult:
    ok: bool
    exit_code: int
    failure_kind: Optional[str]
    log_path: Optional[Path]
    detail: Dict[str, Any]


def _resolve_compose_bin() -> Optional[List[str]]:
    """Locate docker compose CLI. Returns argv prefix or None when missing."""
    if shutil.which("docker") is None:
        return None
    return ["docker", "compose"]


def build_compose_argv(
    *,
    compose_file: str,
    compose_project_name: str,
    action: str,
    volumes: bool = False,
) -> List[str]:
    """Build deterministic argv for docker compose actions.

    Action is one of `up`, `down`, `ps`, `logs`. `up` adds `-d` for detached.
    `down` adds `--volumes` only when explicitly requested.
    """
    if action not in {"up", "down", "ps", "logs"}:
        raise UsageError(f"unsupported compose action: {action}")
    if not compose_file:
        raise UsageError("compose_file is required")
    if not compose_project_name:
        raise UsageError("compose_project_name is required")
    argv: List[str] = [
        "docker",
        "compose",
        "-f",
        compose_file,
        "-p",
        compose_project_name,
        action,
    ]
    if action == "up":
        argv.append("-d")
    if action == "down" and volumes:
        argv.append("--volumes")
    return argv


def doctor_check_docker() -> Dict[str, Any]:
    """Return docker/compose availability info for doctor."""
    docker = shutil.which("docker")
    info: Dict[str, Any] = {
        "docker": docker,
        "compose": None,
    }
    if docker:
        info["compose"] = ["docker", "compose"]
    return info


def doctor_check_sut(
    paths: RuntimePaths,
    sut_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Verify SUT config wiring: compose file exists, healthcheck argv valid."""
    issues: List[str] = []
    warnings: List[str] = []
    mode = sut_cfg.get("mode") or "local"
    compose_file = sut_cfg.get("compose_file")
    if mode not in {"local", "online"}:
        issues.append(f"sut.mode invalid: {mode}")
    if mode == "local" and not compose_file:
        issues.append("compose_file required when sut.mode=local")
    if mode == "online" and compose_file:
        warnings.append("compose_file ignored when sut.mode=online")
    if compose_file and mode == "local":
        cf = paths.repo_root / compose_file
        if not cf.exists():
            issues.append(f"compose_file missing: {compose_file}")
    test_runner = sut_cfg.get("test_runner")
    if test_runner:
        runner = paths.repo_root / str(test_runner).lstrip("./")
        if not runner.exists():
            issues.append(f"test_runner missing: {test_runner}")
        elif runner.is_file() and (runner.stat().st_mode & 0o111) == 0:
            issues.append(f"test_runner not executable: {test_runner}")
    hc = sut_cfg.get("healthcheck") or {}
    cmd = hc.get("command")
    if not isinstance(cmd, list) or not cmd or any(not isinstance(c, str) for c in cmd):
        issues.append("healthcheck.command must be a non-empty argv list")
    openapi = sut_cfg.get("openapi")
    docs = sut_cfg.get("docs")
    tests_dir = sut_cfg.get("tests_dir")
    if not openapi:
        warnings.append("sut.openapi not configured; API generation needs operator decisions")
    if not docs:
        warnings.append("sut.docs not configured; requirements traceability is limited")
    if tests_dir:
        tests_path = paths.repo_root / str(tests_dir)
        if not tests_path.exists():
            warnings.append(f"tests_dir missing: {tests_dir}")
    else:
        warnings.append("sut.tests_dir not configured; generator defaults to tests/")
    web = sut_cfg.get("web") if isinstance(sut_cfg.get("web"), dict) else {}
    api = sut_cfg.get("api") if isinstance(sut_cfg.get("api"), dict) else {}
    if mode == "online":
        if web.get("enabled") and not web.get("url"):
            issues.append("sut.web.url required when sut.mode=online and web.enabled=true")
        if api.get("enabled") and not api.get("url"):
            issues.append("sut.api.url required when sut.mode=online and api.enabled=true")
    return {
        "mode": mode,
        "compose_file": compose_file,
        "test_runner": test_runner,
        "healthcheck_command": list(cmd) if isinstance(cmd, list) else None,
        "issues": issues,
        "warnings": warnings,
    }


def doctor_check_models(
    models_cfg: Dict[str, Any],
    *,
    smoke_timeout_seconds: int = 20,
) -> Dict[str, Any]:
    """Verify each model CLI binary is on PATH and emits the JSON envelope."""
    result: Dict[str, Any] = {}
    issues: List[str] = []
    for role in ("planner", "implementer", "reviewer", "triager"):
        m = models_cfg.get(role) or {}
        cmd = m.get("command") or []
        if not isinstance(cmd, list) or not cmd:
            issues.append(f"models.{role}.command missing or invalid")
            result[role] = None
            continue
        binary = cmd[0]
        located = shutil.which(binary)
        provider = m.get("provider")
        provider_version = _doctor_provider_version(binary) if located else "unknown"
        envelope = {"ok": False, "error": "binary_missing"}
        if located and provider:
            envelope = _doctor_envelope_smoke(
                list(cmd),
                provider=str(provider),
                role=role,
                provider_version=provider_version,
                timeout_seconds=smoke_timeout_seconds,
            )
        result[role] = {
            "command": list(cmd),
            "provider": provider,
            "provider_version": provider_version,
            "found": bool(located),
            "path": located,
            "envelope_smoke": envelope,
        }
        if not located:
            issues.append(f"models.{role} binary not on PATH: {binary}")
        elif not envelope.get("ok"):
            issues.append(f"models.{role} envelope smoke failed: {envelope.get('error')}")
    result["issues"] = issues
    return result


def _doctor_provider_version(binary: str) -> str:
    from .models import _provider_version

    return _provider_version(binary)


def _doctor_envelope_smoke(
    command: List[str],
    *,
    provider: str,
    role: str,
    provider_version: str,
    timeout_seconds: int,
) -> Dict[str, Any]:
    from .models.providers import parse_provider_stdout
    from .models.providers.prompt_suffix import envelope_prompt_suffix

    prompt = (
        "Agentic OS doctor envelope smoke. Return a minimal valid envelope; "
        "no prose is required.\n\n"
        + envelope_prompt_suffix(role)
        + f"\nInvocation provider: {provider}\n"
        + f"Invocation provider_version: {provider_version}\n"
    )
    try:
        proc = stdlib_subprocess.run(
            command,
            input=prompt,
            stdout=stdlib_subprocess.PIPE,
            stderr=stdlib_subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        envelope = parse_provider_stdout(
            provider,
            proc.stdout,
            role=role,
            provider_version=provider_version,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "exit_code": proc.returncode,
        "verdict": envelope.verdict,
        "reason": envelope.reason,
    }


def run_sut_start(
    paths: RuntimePaths,
    events: EventLog,
    *,
    compose_file: Optional[str],
    compose_project_name: Optional[str],
    docker_seconds: int = 240,
) -> SutLifecycleResult:
    """Start the SUT via docker compose up -d."""
    if not compose_file:
        return SutLifecycleResult(
            ok=True,
            exit_code=0,
            failure_kind=None,
            log_path=None,
            detail={"skipped": True, "reason": "compose_file=null"},
        )
    if shutil.which("docker") is None:
        events.write(
            "sut.start_infra_fail",
            severity="error",
            payload={"reason": "docker not on PATH"},
        )
        return SutLifecycleResult(
            ok=False,
            exit_code=INFRA_EXIT_CODE,
            failure_kind="infra_missing_docker",
            log_path=None,
            detail={"reason": "docker not on PATH"},
        )
    compose_path = paths.repo_root / compose_file
    if not compose_path.exists():
        events.write(
            "sut.start_infra_fail",
            severity="error",
            payload={"reason": f"compose_file missing: {compose_file}"},
        )
        return SutLifecycleResult(
            ok=False,
            exit_code=INFRA_EXIT_CODE,
            failure_kind="infra_missing_compose_file",
            log_path=None,
            detail={"reason": f"compose_file missing: {compose_file}"},
        )
    argv = build_compose_argv(
        compose_file=compose_file,
        compose_project_name=compose_project_name or "agentic-sut",
        action="up",
    )
    log_path = paths.subprocess_logs_dir / f"sut-start-{now_iso().replace(':', '-')}.log"
    res = run_command(
        argv,
        cwd=paths.repo_root,
        log_path=log_path,
        timeout_seconds=docker_seconds,
        # Issue #291 — SUT containers never need the operator's model keys.
        include_provider_credentials=False,
    )
    ok = res.exit_code == 0
    events.write(
        "sut.started" if ok else "sut.start_failed",
        severity="info" if ok else "error",
        payload={
            "argv": argv,
            "exit_code": res.exit_code,
            "log_path": str(log_path.relative_to(paths.repo_root)),
        },
    )
    return SutLifecycleResult(
        ok=ok,
        exit_code=0 if ok else INFRA_EXIT_CODE,
        failure_kind=None if ok else "infra_compose_up_failed",
        log_path=log_path,
        detail={"argv": argv, "exit_code": res.exit_code},
    )


def run_sut_healthcheck(
    paths: RuntimePaths,
    events: EventLog,
    *,
    command: Sequence[str],
    timeout_seconds: int,
    retries: int,
) -> SutLifecycleResult:
    """Run the configured healthcheck command with retries.

    Returns ok=True with exit_code=0 on first green probe. After all retries
    fail, returns ok=False with exit_code=2 (infra). Command must be argv list.
    """
    if not isinstance(command, (list, tuple)) or not command:
        raise UsageError("healthcheck command must be a non-empty argv list")
    if timeout_seconds <= 0:
        raise UsageError("healthcheck timeout_seconds must be positive")
    if retries < 0:
        raise UsageError("healthcheck retries must be >= 0")
    log_path = paths.subprocess_logs_dir / f"sut-healthcheck-{now_iso().replace(':', '-')}.log"
    attempts: List[Dict[str, Any]] = []
    for attempt in range(retries + 1):
        res = run_command(
            list(command),
            cwd=paths.repo_root,
            log_path=log_path,
            timeout_seconds=timeout_seconds,
            # Issue #291 — the healthcheck binary is SUT-supplied; deny model keys.
            include_provider_credentials=False,
        )
        attempts.append({"attempt": attempt + 1, "exit_code": res.exit_code})
        if res.exit_code == 0:
            events.write(
                "sut.healthcheck_passed",
                payload={"attempts": attempt + 1, "argv": list(command)},
            )
            return SutLifecycleResult(
                ok=True,
                exit_code=0,
                failure_kind=None,
                log_path=log_path,
                detail={"attempts": attempts},
            )
        if attempt < retries:
            time.sleep(min(1.0, timeout_seconds / 4))
    events.write(
        "sut.healthcheck_failed",
        severity="error",
        payload={"attempts": len(attempts), "argv": list(command)},
    )
    return SutLifecycleResult(
        ok=False,
        exit_code=INFRA_EXIT_CODE,
        failure_kind="infra_healthcheck_timeout",
        log_path=log_path,
        detail={"attempts": attempts},
    )


def run_sut_stop(
    paths: RuntimePaths,
    events: EventLog,
    *,
    compose_file: Optional[str],
    compose_project_name: Optional[str],
    volumes: bool = False,
    docker_seconds: int = 60,
) -> SutLifecycleResult:
    """Stop SUT via docker compose down. Touches only the configured project.

    `--volumes` is only added when explicitly requested by the operator.
    """
    if not compose_file:
        return SutLifecycleResult(
            ok=True,
            exit_code=0,
            failure_kind=None,
            log_path=None,
            detail={"skipped": True, "reason": "compose_file=null"},
        )
    if shutil.which("docker") is None:
        return SutLifecycleResult(
            ok=False,
            exit_code=INFRA_EXIT_CODE,
            failure_kind="infra_missing_docker",
            log_path=None,
            detail={"reason": "docker not on PATH"},
        )
    argv = build_compose_argv(
        compose_file=compose_file,
        compose_project_name=compose_project_name or "agentic-sut",
        action="down",
        volumes=volumes,
    )
    log_path = paths.subprocess_logs_dir / f"sut-stop-{now_iso().replace(':', '-')}.log"
    res = run_command(
        argv,
        cwd=paths.repo_root,
        log_path=log_path,
        timeout_seconds=docker_seconds,
        # Issue #291 — SUT containers never need the operator's model keys.
        include_provider_credentials=False,
    )
    ok = res.exit_code == 0
    events.write(
        "sut.stopped" if ok else "sut.stop_failed",
        severity="info" if ok else "error",
        payload={"argv": argv, "exit_code": res.exit_code, "volumes": volumes},
    )
    return SutLifecycleResult(
        ok=ok,
        exit_code=0 if ok else INFRA_EXIT_CODE,
        failure_kind=None if ok else "infra_compose_down_failed",
        log_path=log_path,
        detail={"argv": argv, "exit_code": res.exit_code, "volumes": volumes},
    )
