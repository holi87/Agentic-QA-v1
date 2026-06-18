"""Gate result + abandon artifact persistence and binding reads.

Split from gates.py (issue #292).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..ids import ulid
from ..paths import RuntimePaths

from .types import GateResult


def write_gate_result(
    paths: RuntimePaths,
    gate: GateResult,
    *,
    name: str,
    patch_metadata: Optional[dict] = None,
) -> Path:
    """Write a gate artifact, optionally binding it to an exact patch.

    Issue #104 — patch resolution must be patch-specific. When `patch_metadata`
    is supplied (a dict with keys `path` and `sha256`), the binding is
    appended after the strict gate body so `parse_gate_output()` keeps
    working and `_read_gate_binding()` can pick it up.
    """
    target = paths.evidence_dir / f"{name}-{ulid()}.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    body = gate.to_text()
    if patch_metadata:
        trailer_lines = []
        if patch_metadata.get("path"):
            trailer_lines.append(f"patch: {patch_metadata['path']}")
        if patch_metadata.get("sha256"):
            trailer_lines.append(f"patch_sha256: {patch_metadata['sha256']}")
        if trailer_lines:
            body = body + "\n".join(trailer_lines) + "\n"
    target.write_text(body, encoding="utf-8")
    return target


def _read_gate_binding(path: Path) -> dict:
    """Return `{verdict, patch_path, patch_sha256}` from a gate artifact.

    Tolerant by design — missing fields default to `None`. The verdict
    is normalized to APPROVE/REJECT/ABANDONED or `None` when unknown.
    """
    out: dict = {"verdict": None, "patch_path": None, "patch_sha256": None}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("verdict:") and out["verdict"] is None:
            value = stripped.split(":", 1)[1].strip()
            if value in {"APPROVE", "REJECT", "ABANDONED"}:
                out["verdict"] = value
        elif stripped.startswith("patch:") and out["patch_path"] is None:
            out["patch_path"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("patch_sha256:") and out["patch_sha256"] is None:
            out["patch_sha256"] = stripped.split(":", 1)[1].strip()
    return out


def write_abandon_artifact(
    paths: RuntimePaths,
    *,
    patch_rel_path: str,
    reason: str,
    operator: str,
) -> Path:
    """Write a gate artifact marking a patch as abandoned by the operator."""
    target = paths.evidence_dir / f"abandon-{ulid()}.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f"verdict: ABANDONED\n"
        f"reason: {reason or 'operator abandoned patch'}\n"
        f"patch: {patch_rel_path}\n"
        f"operator: {operator}\n"
        f"\n"
        f"findings:\n"
        f"- OK:1 - patch abandoned by operator\n"
        f"READY\n"
    )
    target.write_text(body, encoding="utf-8")
    return target


def _artifact_path(paths: RuntimePaths, artifact_path: str) -> Path:
    candidate = Path(artifact_path)
    if candidate.is_absolute():
        return candidate
    return paths.repo_root / candidate
