"""Higher-level façade for running commands with run-record + manifest.

Workflows compose `run_command` (logs+process safety) and orchestrator
bookkeeping (DB rows + events + manifest). This module exposes a single
`run_and_record` helper so future workflows do not duplicate that wiring.

    Thin on purpose: the bulk of the work is in `runtime.subprocess.run_command`
    and `workflows.write_manifest`. Anything heavier belongs in the workflows
    package because it carries policy (which task, which phase, which kind).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .events import EventLog
from .ids import run_id as new_run_id
from .orchestrator import Orchestrator
from .paths import RuntimePaths
from .runtime.subprocess import CommandResult, run_command
from .time_utils import now_iso
from .workflows import env_hash, write_manifest


@dataclass
class RunRecord:
    run_id: str
    log_path: Path
    manifest_path: Path
    result: CommandResult


def run_and_record(
    *,
    orchestrator: Orchestrator,
    paths: RuntimePaths,
    events: EventLog,
    task_id: str,
    kind: str,
    command: Sequence[str],
    timeout_seconds: int,
    shutdown_grace_seconds: int = 5,
    env: Optional[Dict[str, str]] = None,
    extra_manifest: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    include_provider_credentials: bool = True,
    secret_env_names: Sequence[str] = (),
) -> RunRecord:
    """Run a single command for an existing task, record DB rows + manifest.

    The caller owns the task lifecycle (create_task, lease, finish). This
    helper only deals with the one run inside that task.
    """
    rid = new_run_id()
    log_path = paths.subprocess_logs_dir / f"{rid}.log"
    started_at = now_iso()
    argv = _resolve_repo_local_executable(list(command), paths.repo_root)
    orchestrator.record_run(
        task_id=task_id,
        run_id=rid,
        idempotency_key=idempotency_key,
        command=argv,
        cwd=str(paths.repo_root),
        env_hash=env_hash(),
        log_path=str(log_path.relative_to(paths.repo_root)),
        started_at=started_at,
    )
    result = run_command(
        argv,
        cwd=paths.repo_root,
        log_path=log_path,
        timeout_seconds=timeout_seconds,
        shutdown_grace_seconds=shutdown_grace_seconds,
        env=env,
        include_provider_credentials=include_provider_credentials,
        secret_env_names=secret_env_names,
    )
    events.write(
        "runner.command_finished",
        task_id=task_id,
        run_id=rid,
        severity="info" if result.exit_code == 0 else "warning",
        payload={
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "failure_kind": result.failure_kind,
            "log_path": str(log_path.relative_to(paths.repo_root)),
        },
    )
    manifest_path = write_manifest(
        paths=paths,
        run_id_str=rid,
        task_id=task_id,
        kind=kind,
        command=argv,
        cwd=str(paths.repo_root),
        started_at=started_at,
        finished_at=result.finished_at,
        exit_code=result.exit_code,
        failure_kind=result.failure_kind,
        extra=extra_manifest,
    )
    orchestrator.finish_run(
        run_id=rid,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        failure_kind=result.failure_kind,
        unmapped_exit=result.failure_kind == "unknown",
        evidence_path=str((paths.evidence_dir / rid).relative_to(paths.repo_root)),
        manifest_path=str(manifest_path.relative_to(paths.repo_root)),
        finished_at=result.finished_at,
    )
    return RunRecord(run_id=rid, log_path=log_path, manifest_path=manifest_path, result=result)


def _resolve_repo_local_executable(argv: List[str], repo_root: Path) -> List[str]:
    if not argv:
        return argv
    executable = Path(argv[0])
    if executable.is_absolute() or "/" not in argv[0]:
        return argv
    resolved = (repo_root / executable).resolve()
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        return argv
    argv[0] = str(resolved)
    return argv
