"""Top-level work-item analysis orchestration + analyzer note attach.

Split from analysis.py (issue #292).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..atomic_io import atomic_write_json, atomic_write_text
from ..events import EventLog
from ..paths import RuntimePaths
from ..work_items import register_work_item_artifact, update_work_item_status

from .builders import _build_candidate_tests, _build_requirements, _build_risk_map
from .coverage_architect import _apply_coverage_architect
from .inputs import _collect_inputs
from .sut_map import _build_sut_map


def analyze_work_item(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    *,
    work_item_id: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build analysis artefacts (sut-map, requirements, candidates).

    ``session_id`` is opt-in (issue #308): when set, ``models.planner``
    is invoked best-effort after the deterministic analysis lands so
    the autonomous loop records a ``model_invocations`` row keyed to the
    session. The model output is advisory (written beside the canonical
    artefacts as ``ANALYZER-NOTE.md``); the deterministic outputs remain
    the source of truth.
    """
    inputs = _collect_inputs(conn, paths, work_item_id)
    base_dir = paths.runtime_root / "analysis" / work_item_id
    base_dir.mkdir(parents=True, exist_ok=True)

    sut_map = _build_sut_map(paths, inputs)
    # Issue #359 — the probe fan-out runs event/DB-free; the join barrier (this
    # thread, which owns `events`) surfaces any failed probe as a gap event so
    # the degraded map is auditable. Analysis still proceeds on what survived.
    for gap in sut_map.get("probe_gaps") or []:
        events.write(
            "sut_map.probe_gap",
            actor="operator",
            severity="warning",
            payload={
                "work_item_id": work_item_id,
                "probe": gap.get("probe"),
                "error": gap.get("error"),
            },
        )
    requirements_md = _build_requirements(inputs)
    risk_md = _build_risk_map(inputs, sut_map)
    candidates_md, candidates_payload, candidate_summary = _build_candidate_tests(inputs, sut_map)
    # Issue #229 — when autonomy.coverage_architect is on, flip the
    # autonomous-safe subset of candidates to generate_now so the loop
    # does not stall on read-only / documented endpoints.
    _apply_coverage_architect(paths, candidates_payload, candidate_summary, conn=conn)

    atomic_write_json(base_dir / "sut-map.json", sut_map, ensure_ascii=True)
    atomic_write_text(base_dir / "requirements.md", requirements_md)
    atomic_write_text(base_dir / "risk-map.md", risk_md)
    atomic_write_text(base_dir / "candidate-tests.md", candidates_md)
    atomic_write_json(base_dir / "candidate-tests.json", candidates_payload)
    written = [
        base_dir / "sut-map.json",
        base_dir / "requirements.md",
        base_dir / "risk-map.md",
        base_dir / "candidate-tests.md",
        base_dir / "candidate-tests.json",
    ]

    update_work_item_status(conn, events, work_item_id=work_item_id, status="analyzing")
    artifacts: List[Dict[str, Any]] = []
    for path in written:
        rel = str(path.resolve().relative_to(paths.repo_root.resolve()))
        kind = ANALYSIS_KIND_BY_FILENAME[path.name]
        artifacts.append(
            register_work_item_artifact(
                conn,
                paths,
                events,
                work_item_id=work_item_id,
                kind=kind,
                path=rel,
            )
        )

    events.write(
        "work_item.analyzed",
        actor="operator",
        payload={
            "work_item_id": work_item_id,
            "artifacts": [a["path"] for a in artifacts],
            "candidate_summary": candidate_summary,
        },
    )

    # Issue #308 — opt-in model invocation. When called from an
    # autonomous session, drive `models.planner` so the analyse step
    # also lands a `model_invocations` row keyed to session_id. The
    # deterministic artefacts above already shipped; we only attach a
    # side-car ANALYZER-NOTE.md when the call succeeds.
    if session_id:
        try:
            _attach_analyzer_note(
                conn,
                paths,
                events,
                work_item_id=work_item_id,
                session_id=session_id,
                base_dir=base_dir,
                work_item=inputs.work_item,
                requirements_md=requirements_md,
                candidates_md=candidates_md,
            )
        except Exception:
            # Best-effort: must not unwind a successful analysis.
            pass

    return {
        "work_item_id": work_item_id,
        "status": "analyzing",
        "artifacts": artifacts,
        "candidate_summary": candidate_summary,
        "config_warning": inputs.config_warning,
    }


def _attach_analyzer_note(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    *,
    work_item_id: str,
    session_id: str,
    base_dir: Path,
    work_item: Dict[str, Any],
    requirements_md: str,
    candidates_md: str,
) -> None:
    """Issue #308 — drive `models.planner` from inside the analyse step."""
    from ..models.pipeline import try_invoke_role

    prompt = (
        f"# Analysis review for {work_item.get('title', work_item_id)}\n\n"
        f"- task_id: {work_item_id}\n"
        f"- spec: {work_item.get('spec_path', '')}\n"
        f"- sut_root: {work_item.get('sut_root', '')}\n\n"
        "## Requirements (extracted)\n\n"
        f"{requirements_md.rstrip()}\n\n"
        "## Candidate tests (extracted)\n\n"
        f"{candidates_md.rstrip()}\n\n"
        "Flag missing requirement categories and SUT surfaces not yet "
        "covered by candidates.\n"
    )
    result = try_invoke_role(
        conn,
        paths,
        events,
        role="planner",
        prompt=prompt,
        work_item_id=work_item_id,
        session_id=session_id,
    )
    if result is None or not result.output_path:
        return
    src = paths.repo_root / result.output_path
    if not src.exists():
        return
    note = base_dir / "ANALYZER-NOTE.md"
    try:
        note.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        pass


ANALYSIS_KIND_BY_FILENAME = {
    "sut-map.json": "sut_map",
    "requirements.md": "analysis",
    "risk-map.md": "analysis",
    "candidate-tests.md": "analysis",
    "candidate-tests.json": "analysis",
}
