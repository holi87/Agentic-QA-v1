"""Regression: dashboard task action buttons must be state/prereq-aware.

Issue #194 — operators saw every primary action button enabled on a freshly
queued task. The dashboard must:

  - expose a gating map (per-action ``enabled`` flag + human ``reason``) so
    the UI can grey out buttons that have no chance of succeeding;
  - reject POSTs to actions whose prerequisites are not met with HTTP 409
    so the gating is enforced server-side too (defence in depth, in case
    the operator hits the API directly or uses an out-of-date dashboard).

The matrix enforced here:

  * ``analyze``         — always allowed (idempotent re-analysis is fine)
  * ``plan``            — requires an ``analysis`` artifact
  * ``implement-tests`` — requires a ``test_plan`` artifact AND at least one
                          candidate decided as ``generate_now``
  * ``review-gate``     — requires a ``patch`` artifact
  * ``apply-patch``     — requires an APPROVE gate for the patch and no apply artifact yet
  * ``run-tests``       — requires an ``apply`` artifact (tests on disk)
  * ``final-gate``      — requires a ``run`` artifact

The fixtures seed the underlying artifact rows + plan JSON directly rather
than running the real workflows, because the workflows shell out to model
CLIs and disk SUTs that are not available in unit tests.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from agentic_os.atomic_io import atomic_write_json
from agentic_os.events import EventLog
from agentic_os.server import make_server
from agentic_os.storage.db import connect
from agentic_os.work_items import (
    create_work_item_from_payload,
    register_work_item_artifact,
)

from test_dashboard_server import _runtime, _free_port  # type: ignore[import-not-found]


ACTIONS = (
    "analyze",
    "plan",
    "implement-tests",
    "review-gate",
    "apply-patch",
    "run-tests",
    "final-gate",
)


def _start_server(paths) -> Tuple[str, Any, Any]:
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{port}", srv, thread


def _stop_server(srv, thread) -> None:
    srv._shutdown_requested = True  # type: ignore[attr-defined]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _get_json(url: str) -> dict:
    deadline = time.monotonic() + 5
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionRefusedError) as exc:
            last_err = exc
            time.sleep(0.1)
    raise AssertionError(f"server not reachable at {url}: {last_err}")


def _post(url: str, body: Dict[str, Any] | None = None) -> Tuple[int, Dict[str, Any]]:
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = {}
        return exc.code, payload


def _seed_task(paths) -> str:
    conn = connect(paths.db)
    try:
        detail = create_work_item_from_payload(
            conn,
            paths,
            EventLog(conn, paths),
            {
                "title": "Gating regression",
                "priority": "P2",
                "business_goal": "Verify dashboard gating.",
                "expected_behavior": "Buttons enable only when prereqs match.",
            },
        )
    finally:
        conn.close()
    return detail["work_item"]["id"]


def _add_artifact(paths, wid: str, kind: str, rel_path: str) -> None:
    """Register an artifact row for the given task without running a workflow."""
    target = paths.repo_root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(f"# stub {kind}\n", encoding="utf-8")
    conn = connect(paths.db)
    try:
        register_work_item_artifact(
            conn,
            paths,
            EventLog(conn, paths),
            work_item_id=wid,
            kind=kind,
            path=rel_path,
        )
    finally:
        conn.close()


def _add_approve_gate(paths, wid: str, patch_rel: str) -> None:
    gate_rel = f".agentic-os/gates/{wid}-approve.txt"
    target = paths.repo_root / gate_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"verdict: APPROVE\nreason: seeded approval\npatch: {patch_rel}\n",
        encoding="utf-8",
    )
    _add_artifact(paths, wid, "gate", gate_rel)


def _write_plan_json(paths, wid: str, *, with_approved: bool) -> None:
    plan_dir = paths.runtime_root / "plans" / wid
    plan_dir.mkdir(parents=True, exist_ok=True)
    items = [
        {
            "candidate_id": "c1",
            "title": "Health check",
            "decision": "generate_now" if with_approved else "needs_operator_decision",
            "test_type": "api",
            "functional_area": "health",
            "lifecycle_tags": ["smoke"],
            "expected_assertion": "GET /health returns 200",
        }
    ]
    atomic_write_json(plan_dir / "TEST-PLAN.json", {"items": items, "summary": {}})


# ---------------------------------------------------------------------------
# Per-state fixtures
# ---------------------------------------------------------------------------

def _make_state_server(tmp_path: Path, state: str):
    paths = _runtime(tmp_path, enable_write=True)
    wid = _seed_task(paths)
    if state == "queued":
        pass
    elif state == "analyzed":
        _add_artifact(paths, wid, "analysis", f".agentic-os/analysis/{wid}/requirements.md")
    elif state == "planned_no_approved":
        _add_artifact(paths, wid, "analysis", f".agentic-os/analysis/{wid}/requirements.md")
        _add_artifact(paths, wid, "test_plan", f".agentic-os/plans/{wid}/TEST-PLAN.md")
        _write_plan_json(paths, wid, with_approved=False)
    elif state == "planned_with_approved":
        _add_artifact(paths, wid, "analysis", f".agentic-os/analysis/{wid}/requirements.md")
        _add_artifact(paths, wid, "test_plan", f".agentic-os/plans/{wid}/TEST-PLAN.md")
        _write_plan_json(paths, wid, with_approved=True)
    elif state == "patch_ready":
        _add_artifact(paths, wid, "analysis", f".agentic-os/analysis/{wid}/requirements.md")
        _add_artifact(paths, wid, "test_plan", f".agentic-os/plans/{wid}/TEST-PLAN.md")
        _write_plan_json(paths, wid, with_approved=True)
        _add_artifact(paths, wid, "patch", f".agentic-os/patches/{wid}/abc.patch")
    elif state == "approved_pending_apply":
        patch_rel = f".agentic-os/patches/{wid}/abc.patch"
        _add_artifact(paths, wid, "analysis", f".agentic-os/analysis/{wid}/requirements.md")
        _add_artifact(paths, wid, "test_plan", f".agentic-os/plans/{wid}/TEST-PLAN.md")
        _write_plan_json(paths, wid, with_approved=True)
        _add_artifact(paths, wid, "patch", patch_rel)
        _add_approve_gate(paths, wid, patch_rel)
    elif state == "applied":
        patch_rel = f".agentic-os/patches/{wid}/abc.patch"
        _add_artifact(paths, wid, "analysis", f".agentic-os/analysis/{wid}/requirements.md")
        _add_artifact(paths, wid, "test_plan", f".agentic-os/plans/{wid}/TEST-PLAN.md")
        _write_plan_json(paths, wid, with_approved=True)
        _add_artifact(paths, wid, "patch", patch_rel)
        _add_approve_gate(paths, wid, patch_rel)
        _add_artifact(paths, wid, "apply", patch_rel)
    elif state == "run_ready":
        patch_rel = f".agentic-os/patches/{wid}/abc.patch"
        _add_artifact(paths, wid, "analysis", f".agentic-os/analysis/{wid}/requirements.md")
        _add_artifact(paths, wid, "test_plan", f".agentic-os/plans/{wid}/TEST-PLAN.md")
        _write_plan_json(paths, wid, with_approved=True)
        _add_artifact(paths, wid, "patch", patch_rel)
        _add_approve_gate(paths, wid, patch_rel)
        _add_artifact(paths, wid, "apply", patch_rel)
        _add_artifact(paths, wid, "run", f".agentic-os/runs/{wid}/run-1/manifest.json")
    else:
        raise ValueError(f"unknown state: {state}")
    url, srv, thread = _start_server(paths)
    return url, wid, srv, thread


# Expected gating per state for non-trivial actions.
# True = button must be enabled and POST must NOT be blocked by gating.
EXPECTED = {
    "queued": {
        "analyze": True,
        "plan": False,
        "implement-tests": False,
        "review-gate": False,
        "apply-patch": False,
        "run-tests": False,
        "final-gate": False,
    },
    "analyzed": {
        "analyze": True,
        "plan": True,
        "implement-tests": False,
        "review-gate": False,
        "apply-patch": False,
        "run-tests": False,
        "final-gate": False,
    },
    "planned_no_approved": {
        "analyze": True,
        "plan": True,
        "implement-tests": False,  # no candidate approved
        "review-gate": False,
        "apply-patch": False,
        "run-tests": False,
        "final-gate": False,
    },
    "planned_with_approved": {
        "analyze": True,
        "plan": True,
        "implement-tests": True,
        "review-gate": False,
        "apply-patch": False,
        "run-tests": False,
        "final-gate": False,
    },
    "patch_ready": {
        "analyze": True,
        "plan": True,
        "implement-tests": False,  # resolve the generated patch first
        "review-gate": True,
        "apply-patch": False,
        "run-tests": False,  # patch must be applied first
        "final-gate": False,
    },
    "approved_pending_apply": {
        "analyze": True,
        "plan": True,
        "implement-tests": False,
        "review-gate": False,
        "apply-patch": True,
        "run-tests": False,  # patch must be applied first
        "final-gate": False,
    },
    "applied": {
        "analyze": True,
        "plan": True,
        "implement-tests": True,
        "review-gate": True,
        "apply-patch": False,
        "run-tests": True,
        "final-gate": False,
    },
    "run_ready": {
        "analyze": True,
        "plan": True,
        "implement-tests": True,
        "review-gate": True,
        "apply-patch": False,
        "run-tests": True,
        "final-gate": True,
    },
}


@pytest.mark.parametrize("state", list(EXPECTED.keys()))
def test_gating_map_matches_state(tmp_path: Path, state: str) -> None:
    url, wid, srv, thread = _make_state_server(tmp_path, state)
    try:
        gating = _get_json(url + "/api/tasks/" + wid + "/gating")
        assert "actions" in gating, gating
        for action in ACTIONS:
            entry = gating["actions"].get(action)
            assert entry is not None, f"missing gating entry for {action}"
            assert isinstance(entry.get("enabled"), bool), entry
            assert isinstance(entry.get("reason", ""), str), entry
            assert entry["enabled"] == EXPECTED[state][action], (
                f"state={state} action={action} expected={EXPECTED[state][action]} got={entry}"
            )
    finally:
        _stop_server(srv, thread)


@pytest.mark.parametrize("state", list(EXPECTED.keys()))
def test_post_is_blocked_when_gating_disabled(tmp_path: Path, state: str) -> None:
    """Defence in depth: server returns 409 for actions whose prereqs aren't met."""
    url, wid, srv, thread = _make_state_server(tmp_path, state)
    try:
        for action in ACTIONS:
            if action == "analyze":
                # Skipping analyze: it shells out to the model and is also always allowed.
                continue
            if EXPECTED[state][action]:
                # Skipping enabled actions: hitting them would run the real workflow.
                continue
            status, payload = _post(url + "/api/tasks/" + wid + "/" + action)
            assert status == 409, (
                f"state={state} action={action} expected 409 got {status}: {payload}"
            )
            assert payload.get("error") == "action_blocked", payload
            assert payload.get("action") == action, payload
            assert payload.get("reason"), payload
    finally:
        _stop_server(srv, thread)
