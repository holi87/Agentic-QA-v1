"""Patch gate — blocking detection, resolution state, merge-if-approved.

Split from gates.py (issue #292).
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from ..errors import InfraError
from ..events import EventLog
from ..ids import ulid
from ..paths import RuntimePaths
from ..runtime.subprocess import run_command
from ..storage.db import connect as _db_connect

from .io import _artifact_path, _read_gate_binding
from .types import GateFinding, GateResult, PatchMergeResult


def find_patch_gate_violations(
    paths: RuntimePaths,
    *,
    conn: Optional[sqlite3.Connection] = None,
    work_item_id: Optional[str] = None,
) -> List[GateFinding]:
    """Return patch artifacts that do not have a later APPROVE gate artifact."""
    if conn is None and not paths.db.exists():
        return []
    own_conn = conn is None
    db = conn or _db_connect(paths.db)
    try:
        where = "p.kind='patch'"
        params: list[object] = []
        if work_item_id is not None:
            where += " AND p.work_item_id=?"
            params.append(work_item_id)
        rows = db.execute(
            f"""
            SELECT w.id AS work_item_id,
                   w.status AS status,
                   p.path AS patch_path,
                   p.created_at AS patch_created
              FROM work_item_artifacts AS p
              JOIN work_items AS w ON w.id = p.work_item_id
             WHERE {where}
             ORDER BY p.created_at ASC, p.id ASC;
            """,
            params,
        ).fetchall()
        findings: List[GateFinding] = []
        for row in rows:
            if _has_approved_gate_after_patch(
                paths,
                db,
                work_item_id=str(row["work_item_id"]),
                patch_created=str(row["patch_created"]),
                patch_path=str(row["patch_path"]),
            ):
                continue
            state = _resolve_patch_state(
                paths,
                db,
                work_item_id=str(row["work_item_id"]),
                patch_created=str(row["patch_created"]),
                patch_path=str(row["patch_path"]),
            )
            if state == "approved_pending_apply":
                message = (
                    "patch has APPROVE gate but no `apply` artifact — "
                    "patch was reviewed but never applied to the working tree "
                    f"(work_item={row['work_item_id']}, status={row['status']})"
                )
            else:
                message = (
                    "patch artifact requires an APPROVE gate (with apply) or "
                    "ABANDONED verdict before work item can be running/done "
                    f"(work_item={row['work_item_id']}, status={row['status']}, state={state})"
                )
            findings.append(GateFinding(str(row["patch_path"]), 1, message))
        return findings
    except sqlite3.Error as exc:
        return [GateFinding(str(paths.db.relative_to(paths.repo_root)), 1, f"could not inspect patch gates: {exc}")]
    finally:
        if own_conn:
            db.close()


def merge_patch_if_approved(
    *,
    paths: RuntimePaths,
    events: EventLog,
    patch_path: Path,
    gate: GateResult,
    reviewed_patch_sha256: Optional[str] = None,
    work_item_id: Optional[str] = None,
    work_item_title: Optional[str] = None,
) -> PatchMergeResult:
    """Apply a patch only after an APPROVE verdict.

    When `reviewed_patch_sha256` is provided, the on-disk patch is rehashed
    immediately before `git apply` and the result must match. This closes
    the bypass where review-gate approves one diff and the orchestrator
    applies a different patch (issue #109).

    Issue #242 — when `git.enabled=true` in config and `work_item_id` is
    provided, the apply happens inside a per-work-item branch and each
    touched file is autocommitted under `implementer-autopilot`.
    """
    if not gate.approved:
        events.write(
            "gate.patch_blocked",
            severity="warning",
            payload={"reason": gate.reason, "patch_path": str(patch_path)},
        )
        return PatchMergeResult(applied=False, blocked=True, log_path=None)

    if reviewed_patch_sha256 is not None:
        observed = hashlib.sha256(patch_path.read_bytes()).hexdigest()
        if observed != reviewed_patch_sha256:
            events.write(
                "gate.patch_blocked",
                severity="error",
                payload={
                    "reason": "patch_hash_mismatch",
                    "patch_path": str(patch_path),
                    "expected_sha256": reviewed_patch_sha256,
                    "observed_sha256": observed,
                },
            )
            raise InfraError(
                "patch hash mismatch between reviewed diff and apply patch: "
                f"expected {reviewed_patch_sha256}, got {observed}"
            )

    # Issue #242 — start a per-work-item branch on the SUT git repo
    # before applying. Safe no-op when git integration is disabled, git
    # is missing, or sut.root is not a git repo. A dirty base does not
    # halt the loop — we record `branch_blocked` and continue, applying
    # on whatever branch the operator left checked out.
    git_branch_payload: Optional[Dict[str, object]] = None
    if work_item_id is not None:
        try:
            from ..config import load_or_default
            from ..sut_repo import git_start_work_item_branch

            cfg = load_or_default(paths.repo_root).raw
            git_cfg = (cfg.get("git") or {}) if isinstance(cfg, dict) else {}
            if bool(git_cfg.get("enabled")):
                sut_root = (cfg.get("sut") or {}).get("root") or "."
                base = git_cfg.get("origin_branch") or "main"
                branch_res = git_start_work_item_branch(
                    paths,
                    events,
                    sut_root=sut_root,
                    work_item_id=work_item_id,
                    title=work_item_title,
                    base=base,
                )
                detail = branch_res.detail if isinstance(branch_res.detail, dict) else {}
                git_branch_payload = {
                    "branch": detail.get("branch"),
                    "ok": branch_res.ok,
                    "reason": detail.get("reason"),
                }
        except Exception as exc:  # pragma: no cover - best-effort hook
            git_branch_payload = {"ok": False, "error": str(exc)}

    log_path = paths.subprocess_logs_dir / f"gate-apply-{ulid()}.log"
    result = run_command(
        ["git", "apply", str(patch_path)],
        cwd=paths.repo_root,
        log_path=log_path,
        timeout_seconds=60,
    )
    if result.exit_code != 0:
        raise InfraError(f"git apply failed during gate merge; see {log_path}")
    events.write(
        "gate.patch_applied",
        payload={
            "patch_path": str(patch_path),
            "log_path": str(log_path.relative_to(paths.repo_root)),
            "reviewed_patch_sha256": reviewed_patch_sha256,
            "git_branch": git_branch_payload,
        },
    )

    # Issue #242 — autocommit per touched file when the branch hook was
    # entered successfully. We parse the patch for `+++ b/<path>` lines
    # to discover touched files (additions or modifications).
    if (
        work_item_id is not None
        and git_branch_payload is not None
        and git_branch_payload.get("ok")
    ):
        try:
            from ..config import load_or_default
            from ..sut_repo import git_autocommit

            cfg = load_or_default(paths.repo_root).raw
            sut_root = (cfg.get("sut") or {}).get("root") or "."
            touched: List[str] = []
            try:
                patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                patch_text = ""
            for line in patch_text.splitlines():
                if line.startswith("+++ b/"):
                    touched.append(line[len("+++ b/"):])
            if touched:
                git_autocommit(
                    paths,
                    events,
                    sut_root=sut_root,
                    work_item_id=work_item_id,
                    files=touched,
                    title=work_item_title or work_item_id,
                )
        except Exception as exc:  # pragma: no cover - best-effort hook
            events.write(
                "sut.git.autocommit_failed",
                severity="warning",
                payload={"work_item_id": work_item_id, "error": str(exc)},
            )
    return PatchMergeResult(applied=True, blocked=False, log_path=log_path)


def describe_blocking_patches(
    paths: RuntimePaths,
    *,
    conn: Optional[sqlite3.Connection] = None,
    work_item_id: Optional[str] = None,
) -> List[dict]:
    """Return per-patch dashboard payload with resolution state.

    Each entry: {work_item_id, patch_path, state, blocking}. State is one of
    `approved`, `approved_pending_apply`, `abandoned`, `rejected`, `waiting`.
    `waiting`, `rejected`, and `approved_pending_apply` block the final gate.
    """
    if conn is None and not paths.db.exists():
        return []
    own_conn = conn is None
    db = conn or _db_connect(paths.db)
    try:
        where = "p.kind='patch'"
        params: list[object] = []
        if work_item_id is not None:
            where += " AND p.work_item_id=?"
            params.append(work_item_id)
        rows = db.execute(
            f"""
            SELECT w.id AS work_item_id,
                   w.status AS status,
                   p.path AS patch_path,
                   p.created_at AS patch_created
              FROM work_item_artifacts AS p
              JOIN work_items AS w ON w.id = p.work_item_id
             WHERE {where}
             ORDER BY p.created_at ASC, p.id ASC;
            """,
            params,
        ).fetchall()
        result: List[dict] = []
        for row in rows:
            state = _resolve_patch_state(
                paths,
                db,
                work_item_id=str(row["work_item_id"]),
                patch_created=str(row["patch_created"]),
                patch_path=str(row["patch_path"]),
            )
            result.append(
                {
                    "work_item_id": str(row["work_item_id"]),
                    "work_item_status": str(row["status"]),
                    "patch_path": str(row["patch_path"]),
                    "patch_created": str(row["patch_created"]),
                    "state": state,
                    "blocking": state in {"waiting", "rejected", "approved_pending_apply"},
                }
            )
        return result
    finally:
        if own_conn:
            db.close()


def _resolve_patch_state(
    paths: RuntimePaths,
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    patch_created: str,
    patch_path: Optional[str] = None,
) -> str:
    rows = conn.execute(
        """
        SELECT path
          FROM work_item_artifacts
         WHERE work_item_id=?
           AND kind='gate'
           AND created_at >= ?
         ORDER BY created_at ASC, id ASC;
        """,
        (work_item_id, patch_created),
    ).fetchall()
    latest_state = "waiting"
    for row in rows:
        gate_path = _artifact_path(paths, str(row["path"]))
        if not gate_path.is_file():
            continue
        binding = _read_gate_binding(gate_path)
        verdict = binding["verdict"]
        bound = binding["patch_path"]
        # Issue #104 — bound gates only resolve their exact patch.
        if patch_path is not None and bound is not None and bound != patch_path:
            continue
        if verdict == "APPROVE":
            latest_state = "approved"
        elif verdict == "ABANDONED":
            latest_state = "abandoned"
        elif verdict == "REJECT" and latest_state == "waiting":
            latest_state = "rejected"
    # Issue #87 — `approved` without an apply artifact is not a resolution.
    if latest_state == "approved" and not _has_apply_artifact_after_patch(
        conn,
        work_item_id=work_item_id,
        patch_created=patch_created,
        patch_path=patch_path
    ):
        return "approved_pending_apply"
    return latest_state


def _read_resolution_verdict(path: Path) -> Optional[str]:
    """Lenient reader returning APPROVE/REJECT/ABANDONED/None.

    Strict gate output uses APPROVE/REJECT. Operator abandon writes an artifact
    with verdict=ABANDONED — same shape so it can sit next to gate verdicts.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("verdict:"):
            value = stripped.split(":", 1)[1].strip()
            if value in {"APPROVE", "REJECT", "ABANDONED"}:
                return value
            return None
    return None


RESOLVED_VERDICTS = {"APPROVE", "ABANDONED"}


def _has_apply_artifact_after_patch(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    patch_created: str,
    patch_path: Optional[str] = None,
) -> bool:
    """True when an `apply` artifact exists for this work item after the patch.

    `apply` artifacts are written by `merge_patch_if_approved()` only when
    `git apply` succeeded, so they prove the reviewed change reached the
    working tree (issue #87).

    Issue #104 — when `patch_path` is supplied, the apply artifact must
    name the same patch path. This stops a successful apply of patch A
    from resolving a sibling patch B in the same work item.
    """
    if patch_path is not None:
        row = conn.execute(
            """
            SELECT 1
              FROM work_item_artifacts
             WHERE work_item_id=?
               AND kind='apply'
               AND created_at >= ?
               AND path=?
             LIMIT 1;
            """,
            (work_item_id, patch_created, patch_path),
        ).fetchone()
        return row is not None
    row = conn.execute(
        """
        SELECT 1
          FROM work_item_artifacts
         WHERE work_item_id=?
           AND kind='apply'
           AND created_at >= ?
         LIMIT 1;
        """,
        (work_item_id, patch_created),
    ).fetchone()
    return row is not None


def _has_approved_gate_after_patch(
    paths: RuntimePaths,
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    patch_created: str,
    patch_path: Optional[str] = None,
) -> bool:
    """True when **this specific** patch has a resolving outcome.

    Issue #104 — resolution must be patch-specific. A later gate
    artifact only resolves the patch when its `patch:` binding matches
    `patch_path` (when provided). Legacy gate artifacts without a
    binding still resolve any patch from the same work item so the
    pre-#104 history stays valid; new artifacts always carry a binding.

    Issue #87 still applies: APPROVE alone is not enough — an `apply`
    artifact for the same patch path must exist for resolution.
    ABANDONED is a standalone resolution (operator override).
    """
    rows = conn.execute(
        """
        SELECT path
          FROM work_item_artifacts
         WHERE work_item_id=?
           AND kind='gate'
           AND created_at >= ?
         ORDER BY created_at ASC, id ASC;
        """,
        (work_item_id, patch_created),
    ).fetchall()
    saw_approve_for_this_patch = False
    for row in rows:
        gate_path = _artifact_path(paths, str(row["path"]))
        if not gate_path.is_file():
            continue
        binding = _read_gate_binding(gate_path)
        verdict = binding["verdict"]
        bound_path = binding["patch_path"]
        # If the gate is bound to a specific patch, only that exact
        # patch path can be resolved by it. Unbound (legacy) gates
        # still resolve any patch in the same work item.
        if patch_path is not None and bound_path is not None and bound_path != patch_path:
            continue
        if verdict == "ABANDONED":
            return True
        if verdict == "APPROVE":
            saw_approve_for_this_patch = True
    if not saw_approve_for_this_patch:
        return False
    return _has_apply_artifact_after_patch(
        conn,
        work_item_id=work_item_id,
        patch_created=patch_created,
        patch_path=patch_path,
    )
