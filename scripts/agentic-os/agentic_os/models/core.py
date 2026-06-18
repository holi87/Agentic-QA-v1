"""model invocation wrappers.

Runs the planner / implementer / reviewer CLIs as audited subprocess calls.
Every invocation:

- has argv as a Python list (never a shell string);
- writes prompt to `agentic-os-runtime/model-inputs/<id>.txt` (secret-redacted);
- writes model stdout to `agentic-os-runtime/model-outputs/<id>.txt`;
- inserts a row into `model_invocations` with command JSON + exit code;
- raises InfraError with exit hint 2 if the configured binary is missing;
- enforces strict format on reviewer output via `gates.parse_gate_output`.

Operator-provided credentials must arrive as env-var references; the prompt
file is scrubbed of anything that looks like a secret literal before write.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import agentic_os.models as _aut  # run_command monkey-patch surface

from ..errors import InfraError, UsageError
from ..events import EventLog
from ..ids import ulid
from ..paths import RuntimePaths
from ..storage.db import transaction
from ..time_utils import now_iso
from .envelope import EnvelopeError
from .failover import detect_failover_signal, mark_cooldown, resolve_provider_chain
from .providers import parse_provider_stdout
from .providers.prompt_suffix import envelope_prompt_suffix
from .budget import (
    _check_budget_before_call,
    _cost_usd,
    _estimate_tokens,
    _tokens_from_envelope,
)
from .router import _rank_chain_by_quality, _record_swap_success
from .parsing import _provider_version, redact_prompt


@dataclass(frozen=True)
class ModelInvocationResult:
    invocation_id: str
    role: str
    provider: str
    command: List[str]
    input_path: Optional[str]
    output_path: Optional[str]
    exit_code: int
    started_at: str
    finished_at: str
    failure_kind: Optional[str]
    provider_version: str = "unknown"
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


_ALLOWED_ROLES = {"planner", "implementer", "reviewer", "triager"}


_PROVIDERS = {"claude", "codex", "antigravity", "script"}


_ROLE_TO_STEP_PHASE = {
    "planner": "analyze",
    "implementer": "implement",
    "reviewer": "review",
    "triager": "triage",
}


def invoke_model(
    conn,
    paths: RuntimePaths,
    events: EventLog,
    *,
    role: str,
    config: Dict[str, Any],
    prompt: str,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    work_item_id: Optional[str] = None,
    run_id: Optional[str] = None,
    timeout_seconds: int = 600,
    parent_step_id: Optional[str] = None,
) -> ModelInvocationResult:
    """Run the configured model CLI for a role, with provider failover.

    Issue #235 — if the primary attempt emits a known rate-limit / quota /
    auth-error signal in stderr or stdout, mark the provider cold and fall
    back to the next entry in `models.<role>.fallback`. The chain is
    re-resolved from the cooldown table on every call so a still-cold
    provider from an earlier session is skipped automatically.
    """
    if role not in _ALLOWED_ROLES:
        raise UsageError(f"unknown model role: {role!r}")
    models_cfg = config.get("models") if isinstance(config, dict) and "models" in config else config
    budgets_cfg = config.get("budgets") if isinstance(config, dict) else None
    # Issue #270 — transcript capture mode: always | on_block | never.
    autonomy_cfg = config.get("autonomy") if isinstance(config, dict) else None
    transcript_mode = "on_block"
    if isinstance(autonomy_cfg, dict):
        candidate = autonomy_cfg.get("transcript_capture")
        if candidate in ("always", "on_block", "never"):
            transcript_mode = candidate
    model_cfg = (models_cfg.get(role) or {}) if isinstance(models_cfg, dict) else {}
    primary_provider = model_cfg.get("provider")
    primary_command = model_cfg.get("command")
    if primary_provider not in _PROVIDERS:
        raise UsageError(f"models.{role}.provider invalid: {primary_provider!r}")
    if not isinstance(primary_command, list) or not primary_command or any(
        not isinstance(c, str) for c in primary_command
    ):
        raise UsageError(f"models.{role}.command must be a non-empty argv list")

    role_signals = list(model_cfg.get("fallback_signals") or [])
    role_cooldown = model_cfg.get("cooldown_seconds")

    fallback_entries = model_cfg.get("fallback") or []
    chain = resolve_provider_chain(
        primary=dict(model_cfg),
        fallback=fallback_entries,
        conn=conn,
        role=role,
    )
    chain = _rank_chain_by_quality(conn, events, role=role, chain=chain)

    # Budget preflight must price the provider that will actually run first.
    # A `provider_quality` learning can re-rank a costlier fallback ahead of
    # the configured primary, so estimate against the ranked head (#273).
    first_provider = str((chain[0].get("provider") if chain else None) or primary_provider)
    estimated_tokens_in = _estimate_tokens(prompt)
    _check_budget_before_call(
        conn,
        events,
        budgets_cfg if isinstance(budgets_cfg, dict) else {},
        role=role,
        session_id=session_id,
        task_id=task_id,
        run_id=run_id,
        estimated_tokens=estimated_tokens_in,
        repo_root=paths.repo_root,
        provider=first_provider,
    )

    last_result: Optional[ModelInvocationResult] = None
    last_signal_detail: Optional[str] = None
    for idx, entry in enumerate(chain):
        provider = entry.get("provider")
        command = entry.get("command")
        if provider not in _PROVIDERS or not isinstance(command, list) or not command:
            # Skip malformed fallback entries (still validated upstream).
            continue
        binary = command[0]
        if shutil.which(binary) is None:
            events.write(
                "model.binary_missing",
                severity="error",
                payload={"role": role, "provider": provider, "binary": binary},
            )
            if idx + 1 < len(chain):
                events.write(
                    "provider_failover",
                    severity="warning",
                    actor=f"{role}-failover",
                    payload={
                        "role": role,
                        "from": provider,
                        "to": chain[idx + 1].get("provider"),
                        "trigger": "binary_missing",
                    },
                )
                continue
            raise InfraError(f"models.{role} binary not on PATH: {binary}")
        signals = list(entry.get("fallback_signals") or []) + role_signals
        cooldown_seconds = entry.get("cooldown_seconds")
        if cooldown_seconds is None:
            cooldown_seconds = role_cooldown
        attempt = _invoke_attempt(
            conn,
            paths,
            events,
            role=role,
            provider=str(provider),
            command=list(command),
            model_role=str(entry.get("role", role)),
            base_prompt=prompt,
            estimated_tokens_in=estimated_tokens_in,
            session_id=session_id,
            task_id=task_id,
            work_item_id=work_item_id,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
            parent_step_id=parent_step_id,
            transcript_capture=transcript_mode,
        )
        last_result = attempt.result
        signal = detect_failover_signal(
            stdout=attempt.stdout,
            stderr=attempt.stderr,
            exit_code=attempt.result.exit_code,
            extra_signals=signals,
        )
        if signal.matched and idx + 1 < len(chain):
            cooldown_until = mark_cooldown(
                conn,
                role=role,
                provider=str(provider),
                trigger=signal.trigger,
                cooldown_seconds=int(cooldown_seconds) if isinstance(cooldown_seconds, (int, float)) else 600,
            )
            next_provider = chain[idx + 1].get("provider")
            events.write(
                "provider_failover",
                severity="warning",
                actor=f"{role}-failover",
                payload={
                    "role": role,
                    "from": provider,
                    "to": next_provider,
                    "trigger": signal.trigger,
                    "detail": signal.detail,
                    "cooldown_until": cooldown_until,
                },
            )
            last_signal_detail = signal.detail
            continue
        if idx > 0 and attempt.result.exit_code == 0:
            # A fallback provider genuinely succeeded (exit 0) after a failover
            # swap — record the rescue so the router prefers it next time (#273).
            # A non-failover failure (e.g. gate reject exit 1) must NOT count as
            # quality, or routing would learn to prefer a failing provider.
            _record_swap_success(
                conn,
                role=role,
                provider=str(provider),
                rescued_from=str(chain[idx - 1].get("provider")),
                trigger=last_signal_detail,
            )
        return attempt.result

    # Chain exhausted — either all attempts hit failover signals, or the last
    # attempt completed (cleanly or not). Surface the exhaustion event when
    # the last attempt also matched a signal so operators can escalate.
    if last_result is None:
        raise InfraError(f"models.{role}: no usable provider in chain")
    events.write(
        "provider_chain_exhausted",
        severity="error",
        actor=f"{role}-failover",
        payload={
            "role": role,
            "last_provider": last_result.provider,
            "last_exit_code": last_result.exit_code,
            "last_signal_detail": last_signal_detail,
        },
    )
    return last_result


@dataclass(frozen=True)
class _AttemptResult:
    result: ModelInvocationResult
    stdout: str
    stderr: str


def _invoke_attempt(
    conn,
    paths: RuntimePaths,
    events: EventLog,
    *,
    role: str,
    provider: str,
    command: List[str],
    model_role: str,
    base_prompt: str,
    estimated_tokens_in: int,
    session_id: Optional[str],
    task_id: Optional[str],
    work_item_id: Optional[str] = None,
    run_id: Optional[str],
    timeout_seconds: int,
    parent_step_id: Optional[str],
    transcript_capture: str = "never",
) -> "_AttemptResult":
    """Single provider attempt; emits one step.start / step.end + DB row."""
    provider_version = _provider_version(command[0])

    invocation_id = ulid()
    inputs_dir = paths.runtime_root / "model-inputs"
    outputs_dir = paths.runtime_root / "model-outputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    input_path = inputs_dir / f"{invocation_id}.txt"
    output_path = outputs_dir / f"{invocation_id}.txt"

    # Skill injection picks the provider-specific skill bundle (issue #235
    # acceptance — failover re-resolves skills/{provider}/).
    skill_resolution = None
    prompt = base_prompt
    try:
        from ..skills import compose_prompt, load_skills_config

        skills_cfg = load_skills_config(paths.repo_root / "config" / "skills.yml")
        prompt, skill_resolution = compose_prompt(
            role,
            base_prompt,
            config=skills_cfg,
            project_root=paths.repo_root,
            provider=provider,
        )
    except Exception as exc:
        events.write(
            "skill.injection_failed",
            severity="warning",
            payload={"role": role, "provider": provider, "error": str(exc)},
        )

    # Architecture context injection (issue #293). Prepended ahead of skills so
    # the agent reads the map first; best-effort and budget-bounded, a failure
    # never blocks the invocation.
    try:
        from ..architecture_context import (
            DEFAULT_BUDGET_TOKENS,
            architecture_context_block,
        )

        enabled = True
        budget = DEFAULT_BUDGET_TOKENS
        # Config read is optional: a missing config must not disable injection,
        # so fall back to the enabled defaults rather than skipping the block.
        try:
            from ..config import load_or_default

            prompt_ctx_cfg = (
                load_or_default(paths.repo_root).raw.get("prompt_context") or {}
            )
            enabled = bool(prompt_ctx_cfg.get("architecture_enabled", True))
            budget = int(
                prompt_ctx_cfg.get("architecture_budget_tokens", DEFAULT_BUDGET_TOKENS)
            )
        except Exception:
            pass
        if enabled:
            arch_block = architecture_context_block(
                paths.repo_root, budget_tokens=budget
            )
            if arch_block:
                prompt = arch_block + "\n" + prompt
    except Exception as exc:
        events.write(
            "architecture.injection_failed",
            severity="warning",
            payload={"role": role, "provider": provider, "error": str(exc)},
        )

    # Learnings context injection (issue #287). Prepended after the
    # architecture block, for planner + implementer only — the reviewer /
    # triager read the store at their own decision sites. Best-effort and
    # budget-bounded; a failure emits a warning and never blocks invocation.
    try:
        from .. import learnings_context as _lc

        enabled = True
        budget = _lc.DEFAULT_BUDGET_TOKENS
        try:
            from ..config import load_or_default

            prompt_ctx_cfg = (
                load_or_default(paths.repo_root).raw.get("prompt_context") or {}
            )
            enabled = bool(prompt_ctx_cfg.get("learnings_enabled", True))
            budget = int(
                prompt_ctx_cfg.get("learnings_budget_tokens", _lc.DEFAULT_BUDGET_TOKENS)
            )
        except Exception:
            pass
        if enabled:
            learnings_block = _lc.learnings_context_block(
                conn, role=role, budget_tokens=budget
            )
            if learnings_block:
                prompt = learnings_block + "\n" + prompt
                try:
                    data = _lc.relevant_learnings(conn)
                    used_kinds = [k for k, v in data.items() if v]
                    used_subjects = (
                        list(data.get("flaky") or [])
                        + [i.get("subject") for i in (data.get("coverage_gap") or [])]
                        + [i.get("subject") for i in (data.get("skill_failure") or [])]
                    )
                    events.write(
                        "learning.consulted",
                        actor=role,
                        payload={
                            "kinds": used_kinds,
                            "subjects": used_subjects,
                            "provider": provider,
                        },
                    )
                except Exception:
                    pass
    except Exception as exc:
        events.write(
            "learning.injection_failed",
            severity="warning",
            payload={"role": role, "provider": provider, "error": str(exc)},
        )

    # Memory context injection (issue #289). Prepended after the learnings
    # block, so prompt order is architecture → learnings → memory (each
    # prepend puts the latest block highest). Planner + implementer only;
    # scoped to the active project so projects never mix. The recall query is
    # the base prompt. Best-effort and budget-bounded; a failure emits a
    # warning and never blocks invocation.
    try:
        from .. import memory_context as _mc

        enabled = True
        budget = _mc.DEFAULT_BUDGET_TOKENS
        top_k = _mc.DEFAULT_TOP_K
        try:
            from ..config import load_or_default

            prompt_ctx_cfg = (
                load_or_default(paths.repo_root).raw.get("prompt_context") or {}
            )
            enabled = bool(prompt_ctx_cfg.get("memory_enabled", True))
            budget = int(
                prompt_ctx_cfg.get("memory_budget_tokens", _mc.DEFAULT_BUDGET_TOKENS)
            )
            top_k = int(prompt_ctx_cfg.get("memory_top_k", _mc.DEFAULT_TOP_K))
        except Exception:
            pass
        if enabled:
            # Resolve the active project best-effort; never block invocation.
            project_id = None
            try:
                from ..projects import resolve_active_project_id

                cfg = None
                try:
                    from ..config import load_or_default

                    cfg = load_or_default(paths.repo_root)
                except Exception:
                    cfg = None
                project_id = resolve_active_project_id(conn, cfg)
            except Exception:
                project_id = None
            if project_id is not None:
                memory_block = _mc.memory_context_block(
                    conn,
                    project_id=project_id,
                    role=role,
                    text=base_prompt,
                    budget_tokens=budget,
                    top_k=top_k,
                )
                if memory_block:
                    prompt = memory_block + "\n" + prompt
                    try:
                        from ..memory import query_memory

                        used = query_memory(
                            conn, project_id=project_id, text=base_prompt, limit=top_k
                        )
                        events.write(
                            "memory.consulted",
                            actor=role,
                            payload={
                                "project_id": project_id,
                                "source_ids": [
                                    [u["source"], u["source_id"]] for u in used
                                ],
                                "scores": [u["score"] for u in used],
                                "provider": provider,
                            },
                        )
                    except Exception:
                        pass
    except Exception as exc:
        events.write(
            "memory.injection_failed",
            severity="warning",
            payload={"role": role, "provider": provider, "error": str(exc)},
        )

    prompt = (
        prompt.rstrip()
        + "\n\n"
        + envelope_prompt_suffix(role)
        + f"\n\nInvocation provider: {provider}\n"
        + f"Invocation provider_version: {provider_version}\n"
    )
    redacted = redact_prompt(prompt)
    input_path.write_text(redacted, encoding="utf-8")

    started_at = now_iso()
    log_path = paths.subprocess_logs_dir / f"model-{invocation_id}.log"
    rel_log = (
        str(log_path.relative_to(paths.repo_root))
        if _is_under(log_path, paths.repo_root)
        else str(log_path)
    )
    step_phase = _ROLE_TO_STEP_PHASE.get(role, "analyze")
    step_id = events.start_step(
        kind=role,
        phase=step_phase,
        actor=f"{provider}-autopilot",
        role=role,
        provider=provider,
        skill=(skill_resolution.skills[0].skill_id if skill_resolution and skill_resolution.skills else None),
        work_item_id=task_id,
        parent_step_id=parent_step_id,
        detail=f"invocation={invocation_id}",
        log_ref=rel_log,
        run_id=run_id,
        task_id=task_id,
    )
    # Codex PR #276 review (P1) — every started step must be terminated. The
    # block below carries every code path that can raise (subprocess spawn,
    # envelope parsing, DB writes). On exception we close the step with
    # outcome=failed before re-raising so the dashboard never shows a
    # permanently-running step.
    step_terminated = False
    final_outcome = "failed"
    final_exit_code: Optional[int] = None
    final_detail = f"invocation={invocation_id}"
    tokens_in = 0
    tokens_out = 0
    try:
        expanded_command = [
            c.replace("{prompt_file}", str(input_path)) for c in command
        ]
        stdin_payload: Optional[str] = None
        if expanded_command == list(command):
            stdin_payload = redacted
        res = _aut.run_command(
            expanded_command,
            cwd=paths.repo_root,
            log_path=log_path,
            timeout_seconds=timeout_seconds,
            input_text=stdin_payload,
        )
        finished_at = now_iso()
        raw_log = log_path.read_text(encoding="utf-8", errors="replace")
        stdout_text = "\n".join(
            line[len("[stdout] "):]
            for line in raw_log.splitlines()
            if line.startswith("[stdout] ")
        )
        stderr_text = "\n".join(
            line[len("[stderr] "):]
            for line in raw_log.splitlines()
            if line.startswith("[stderr] ")
        )
        output_path.write_text(stdout_text, encoding="utf-8")
        envelope = None
        envelope_error: Optional[str] = None
        try:
            envelope = parse_provider_stdout(
                provider,
                stdout_text,
                role=role,
                provider_version=provider_version,
            )
        except EnvelopeError as exc:
            envelope_error = str(exc)
        tokens_in, tokens_out = _tokens_from_envelope(
            envelope,
            fallback_in=estimated_tokens_in,
            fallback_out=_estimate_tokens(stdout_text),
        )
        cost_usd = _cost_usd(paths.repo_root, provider, tokens_in=tokens_in, tokens_out=tokens_out)

        rel_input = str(input_path.relative_to(paths.repo_root))
        rel_output = str(output_path.relative_to(paths.repo_root))

        with transaction(conn):
            conn.execute(
                """
                INSERT INTO model_invocations(
                    id, session_id, task_id, work_item_id, run_id, model_role,
                    provider, command, input_path, output_path, exit_code,
                    started_at, finished_at, provider_version,
                    tokens_in, tokens_out, cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    invocation_id,
                    session_id,
                    task_id,
                    work_item_id,
                    run_id,
                    model_role,
                    provider,
                    json.dumps(list(command)),
                    rel_input,
                    rel_output,
                    res.exit_code,
                    started_at,
                    finished_at,
                    provider_version,
                    tokens_in,
                    tokens_out,
                    cost_usd,
                ),
            )
        failure_kind: Optional[str] = None if res.exit_code == 0 else "model_nonzero_exit"
        events.write(
            "model.invoked" if res.exit_code == 0 else "model.failed",
            severity="info" if res.exit_code == 0 else "warning",
            payload={
                "id": invocation_id,
                "role": role,
                "provider": provider,
                "provider_version": provider_version,
                "exit_code": res.exit_code,
                "input_path": rel_input,
                "output_path": rel_output,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": cost_usd,
                "envelope_valid": envelope_error is None,
                "envelope_error": envelope_error,
                "skills_injected": [s.skill_id for s in (skill_resolution.skills if skill_resolution else [])],
            },
        )
        if skill_resolution and skill_resolution.skills:
            for skill in skill_resolution.skills:
                events.write(
                    "skill.injected",
                    payload={
                        "invocation_id": invocation_id,
                        "skill_id": skill.skill_id,
                        "checksum": skill.checksum,
                    },
                )
        final_outcome = "ok" if res.exit_code == 0 else "failed"
        final_exit_code = res.exit_code
        final_detail = f"invocation={invocation_id} tokens_in={tokens_in} tokens_out={tokens_out}"
        events.end_step(
            step_id,
            outcome=final_outcome,
            kind=role,
            phase=step_phase,
            actor=f"{provider}-autopilot",
            role=role,
            provider=provider,
            work_item_id=task_id,
            parent_step_id=parent_step_id,
            detail=final_detail,
            log_ref=rel_log,
            exit_code=final_exit_code,
            run_id=run_id,
            task_id=task_id,
        )
        step_terminated = True
        # Issue #270 — capture the structured reasoning transcript. Gated by
        # `transcript_capture` (never = zero-overhead, returns immediately).
        try:
            from ..transcripts import capture_transcript

            capture_transcript(
                conn,
                events,
                mode=transcript_capture,
                outcome=final_outcome,
                invocation_id=invocation_id,
                step_id=step_id,
                envelope=envelope,
                stdout_text=stdout_text,
                redactor=redact_prompt,
            )
        except Exception:  # pragma: no cover - transcript capture must not break invocation
            pass
    finally:
        if not step_terminated:
            try:
                events.end_step(
                    step_id,
                    outcome="failed",
                    kind=role,
                    phase=step_phase,
                    actor=f"{provider}-autopilot",
                    role=role,
                    provider=provider,
                    work_item_id=task_id,
                    parent_step_id=parent_step_id,
                    detail=f"{final_detail} (terminated by exception)",
                    log_ref=rel_log,
                    exit_code=final_exit_code,
                    run_id=run_id,
                    task_id=task_id,
                )
            except Exception:
                # Best-effort — never mask the original exception with
                # an end_step write failure.
                pass

    result = ModelInvocationResult(
        invocation_id=invocation_id,
        role=role,
        provider=provider,
        command=list(command),
        input_path=rel_input,
        output_path=rel_output,
        exit_code=res.exit_code,
        started_at=started_at,
        finished_at=finished_at,
        failure_kind=failure_kind,
        provider_version=provider_version,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
    )
    return _AttemptResult(result=result, stdout=stdout_text, stderr=stderr_text)


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False
