"""Best-effort `invoke_model` hooks for deterministic pipeline builders.

Issue #308 — `analyze_work_item` / `plan_work_item` /
`implement_tests_for_work_item` were deterministic artefact builders with
zero model calls, leaving `model_invocations.cost_usd` always at 0 during
autonomous runs and blocking #290 children 3–5 (decision auto-completion,
skill failover, cost-prediction early abort).

This module wires the autonomous loop into ``invoke_model`` without
changing the contract of the deterministic builders:

- the call is *opt-in* via a non-empty ``session_id`` (CLI / dashboard
  one-shot callers pass nothing → no extra row, no extra event);
- the canonical config is loaded best-effort, and a missing
  ``models.<role>`` entry / missing binary is a silent skip;
- any exception is logged and swallowed — the deterministic builder
  remains the source of truth for plans, analysis artefacts and patches.

The function delegates to :func:`agentic_os.models.invoke_model`, so
provider failover (#235), `_rank_chain_by_quality` (#273), skill
injection (#235 acceptance), envelope parsing and budget pre-flight all
still apply.
"""
from __future__ import annotations

import shutil
from typing import Any, Dict, Optional

from ..events import EventLog
from ..paths import RuntimePaths
from . import ModelInvocationResult, invoke_model


def try_invoke_role(
    conn: Any,
    paths: RuntimePaths,
    events: EventLog,
    *,
    role: str,
    prompt: str,
    work_item_id: str,
    session_id: Optional[str],
) -> Optional[ModelInvocationResult]:
    """Run ``invoke_model`` for ``role`` if the session opted in.

    Returns ``None`` (and emits ``model.invoke_skipped`` for diagnosable
    skip reasons) when the call cannot or should not run. Returns the
    :class:`ModelInvocationResult` on success so the caller can persist
    the model output beside its deterministic artefact.
    """
    if not session_id:
        return None
    try:
        from ..config import ConfigError, load_or_default
    except Exception:
        return None
    try:
        cfg = load_or_default(paths.repo_root)
    except ConfigError:
        return None
    raw = getattr(cfg, "raw", None)
    if not isinstance(raw, dict):
        return None
    models_cfg = raw.get("models")
    if not isinstance(models_cfg, dict) or not isinstance(models_cfg.get(role), dict):
        return None
    role_cfg = models_cfg[role]
    command = role_cfg.get("command")
    if not isinstance(command, list) or not command:
        return None
    # Skip when the primary binary is not on PATH; invoke_model would
    # walk the fallback chain but in a pipeline hook we prefer a silent
    # skip over a failover storm on every plan step.
    if shutil.which(str(command[0])) is None:
        _emit_skip(
            events,
            role=role,
            work_item_id=work_item_id,
            session_id=session_id,
            reason="primary_binary_missing",
        )
        return None
    # Issue #339 — write through the new `work_item_id` column so the
    # autonomous-pipeline row resolves to its work item at the SQL
    # level. `task_id` (FK to `tasks(id)`) stays NULL for these rows
    # because the autonomous loop never creates a `tasks` entry; the
    # older execution path (runs.task_id chain) continues to use it.
    try:
        return invoke_model(
            conn,
            paths,
            events,
            role=role,
            config=raw,
            prompt=prompt,
            session_id=session_id,
            work_item_id=work_item_id,
        )
    except Exception as exc:
        _emit_skip(
            events,
            role=role,
            work_item_id=work_item_id,
            session_id=session_id,
            reason=f"invoke_failed: {exc.__class__.__name__}: {exc}",
        )
        return None


def _emit_skip(
    events: EventLog,
    *,
    role: str,
    work_item_id: str,
    session_id: Optional[str],
    reason: str,
) -> None:
    try:
        events.write(
            "model.invoke_skipped",
            severity="warning",
            actor=role,
            payload={
                "role": role,
                "work_item_id": work_item_id,
                "session_id": session_id,
                "reason": reason,
            },
        )
    except Exception:
        # Pipeline must not fail because of telemetry; the deterministic
        # builder owns correctness.
        pass
