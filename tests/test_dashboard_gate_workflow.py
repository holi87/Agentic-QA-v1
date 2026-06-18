"""Dashboard review-gate, run-tests, final-gate, and file-serving integration workflow."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict

import pytest

from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db
from agentic_os.work_items import register_work_item_artifact

from test_dashboard_server import _runtime, _free_port  # type: ignore[import-not-found]


@pytest.fixture
def writable_dashboard(tmp_path: Path):
    paths = _runtime(tmp_path, enable_write=True)
    _seed_test_runner(paths.repo_root)
    _seed_report_scripts(paths.repo_root)
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    yield base, paths
    srv._shutdown_requested = True  # type: ignore[attr-defined]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _seed_test_runner(repo: Path) -> None:
    runner = repo / "run-tests.sh"
    runner.write_text(
        "#!/usr/bin/env bash\n"
        "mkdir -p build/test-results/test reports\n"
        "cat > build/test-results/test/TEST-fake.xml <<'XML'\n"
        '<testsuite name="fake" tests="1" failures="0" errors="0" skipped="0">\n'
        '  <testcase classname="fake" name="ok"/>\n'
        "</testsuite>\n"
        "XML\n"
        "exit 0\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)


def _seed_report_scripts(repo: Path) -> None:
    """Stub the report finalization scripts the runner shells out to.

    Phase 14 needs reports/last-run.json and reports/summary.md present after
    a green run; phase 07 wires the real scripts but for a hermetic dashboard
    test we synthesize the artifacts the orchestrator expects to see.
    """
    scripts = repo / "scripts"
    scripts.mkdir(exist_ok=True)
    for name, body in (
        ("copy-reports.sh",
         "#!/usr/bin/env bash\nmkdir -p reports\nexit 0\n"),
        ("extract-last-run.sh",
         "#!/usr/bin/env bash\nmkdir -p reports\n"
         "printf '{\"status\": \"green\"}\\n' > reports/last-run.json\nexit 0\n"),
        ("build-summary.sh",
         "#!/usr/bin/env bash\nmkdir -p reports\n"
         "printf 'fake summary\\n' > reports/summary.md\nexit 0\n"),
    ):
        path = scripts / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)


def _create_work_item(base: str) -> Dict[str, Any]:
    body = json.dumps(
        {
            "title": "Phase 14 e2e",
            "priority": "P2",
            "business_goal": "exercise dashboard run flow",
            "expected_behavior": "endpoints return exit_code mapping",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        base + "/api/tasks",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=3) as resp:
        return json.loads(resp.read().decode("utf-8"))["work_item"]


def _post_json(base: str, path: str, body: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        exc.add_note(f"response body: {body_text}")  # type: ignore[attr-defined]
        raise


def _seed_artifact(paths: RuntimePaths, work_item_id: str, *, kind: str, rel_path: str) -> None:
    """Issue #194 — register a prerequisite artifact for gating-aware endpoints.

    The dashboard now blocks `run-tests`/`final-gate`/etc with HTTP 409 when
    the prior step's artifact is missing. Phase 14 tests intentionally
    exercise these endpoints without running the previous workflow, so they
    must stub the artifact row directly.
    """
    target = paths.repo_root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(f"# stub {kind}\n", encoding="utf-8")
    conn = init_db(paths.db)
    try:
        register_work_item_artifact(
            conn,
            paths,
            EventLog(conn, paths),
            work_item_id=work_item_id,
            kind=kind,
            path=rel_path,
        )
    finally:
        conn.close()


def _seed_run_prereqs(paths: RuntimePaths, work_item_id: str) -> None:
    """Seed enough artifacts so the dashboard allows `run-tests`."""
    _seed_artifact(
        paths,
        work_item_id,
        kind="apply",
        rel_path=f".agentic-os/patches/{work_item_id}.apply.json",
    )


def _seed_final_gate_prereqs(paths: RuntimePaths, work_item_id: str) -> None:
    """Seed enough artifacts so the dashboard allows `final-gate`."""
    _seed_artifact(
        paths,
        work_item_id,
        kind="run",
        rel_path=f".agentic-os/runs/{work_item_id}/manifest.json",
    )


def _seed_patch_artifact(paths: RuntimePaths, work_item_id: str) -> Path:
    paths.patches_dir.mkdir(parents=True, exist_ok=True)
    patch = paths.patches_dir / f"{work_item_id}.patch"
    patch.write_text(
        "diff --git a/tests/api/test_foo.py b/tests/api/test_foo.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/api/test_foo.py\n"
        "@@\n"
        "+def test_foo():\n"
        "+    assert True\n",
        encoding="utf-8",
    )
    conn = init_db(paths.db)
    try:
        events = EventLog(conn, paths)
        register_work_item_artifact(
            conn,
            paths,
            events,
            work_item_id=work_item_id,
            kind="patch",
            path=str(patch.resolve().relative_to(paths.repo_root.resolve())),
        )
    finally:
        conn.close()
    return patch


def _seed_failing_test_runner(repo: Path) -> None:
    runner = repo / "run-tests.sh"
    runner.write_text(
        "#!/usr/bin/env bash\n"
        "mkdir -p build/test-results/test reports\n"
        "cat > build/test-results/test/TEST-fake.xml <<'XML'\n"
        '<testsuite name="fake" tests="1" failures="1" errors="0" skipped="0">\n'
        '  <testcase classname="fake" name="ok"><failure/></testcase>\n'
        "</testsuite>\n"
        "XML\n"
        "exit 1\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)


def test_run_tests_failure_marks_work_item_failed(tmp_path: Path) -> None:
    paths = _runtime(tmp_path, enable_write=True)
    _seed_failing_test_runner(paths.repo_root)
    _seed_report_scripts(paths.repo_root)
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        time.sleep(0.1)
        work_item = _create_work_item(base)
        # Issue #194 — `run-tests` is gated on an `apply` artifact now.
        _seed_run_prereqs(paths, work_item["id"])
        payload = _post_json(base, f"/api/tasks/{work_item['id']}/run-tests")
        assert payload["exit_code"] == 1
        assert payload["failure_kind"] == "product"
        detail = json.loads(
            urllib.request.urlopen(
                base + f"/api/tasks/{work_item['id']}", timeout=3
            ).read().decode("utf-8")
        )
        # status must reflect the failure, not flip back to "running"
        assert detail["work_item"]["status"] == "failed"
    finally:
        srv._shutdown_requested = True  # type: ignore[attr-defined]
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def test_run_tests_endpoint_attaches_artifacts(writable_dashboard) -> None:
    base, paths = writable_dashboard
    work_item = _create_work_item(base)
    # Issue #194 — `run-tests` is gated on an `apply` artifact now.
    _seed_run_prereqs(paths, work_item["id"])
    payload = _post_json(base, f"/api/tasks/{work_item['id']}/run-tests")
    assert payload["exit_code"] == 0
    assert payload["failure_kind"] is None
    assert payload["reports_path"] == "reports"
    detail = json.loads(
        urllib.request.urlopen(
            base + f"/api/tasks/{work_item['id']}", timeout=3
        ).read().decode("utf-8")
    )
    kinds = {a["kind"] for a in detail["artifacts"]}
    assert {"run", "evidence", "report"} <= kinds


def test_review_gate_endpoint_blocks_without_patch(writable_dashboard) -> None:
    base, _paths = writable_dashboard
    work_item = _create_work_item(base)
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(base, f"/api/tasks/{work_item['id']}/review-gate")
    # Issue #194 — gating now rejects the request with 409 before the
    # workflow-level patch check would have returned 400. Both shapes are
    # acceptable per the dashboard contract; the test pins the new
    # gating-aware response so a regression to the old "let the workflow
    # complain" path is caught.
    assert exc.value.code == 409


def test_review_gate_endpoint_runs_with_patch(writable_dashboard) -> None:
    base, paths = writable_dashboard
    work_item = _create_work_item(base)
    _seed_patch_artifact(paths, work_item["id"])
    payload = _post_json(base, f"/api/tasks/{work_item['id']}/review-gate")
    assert payload["exit_code"] in {0, 1}
    assert payload["task_id"]
    assert payload["scope"] == "assertion"


def test_final_gate_endpoint_returns_exit_code(writable_dashboard) -> None:
    base, paths = writable_dashboard
    work_item = _create_work_item(base)
    # Issue #194 — `final-gate` is gated on a `run` artifact now.
    _seed_final_gate_prereqs(paths, work_item["id"])
    payload = _post_json(base, f"/api/tasks/{work_item['id']}/final-gate")
    # without a successful run-tests on this branch the final gate is expected
    # to reject (exit 1), but the contract is: response carries exit_code and
    # the runtime is still reachable afterwards.
    assert payload["exit_code"] in {0, 1}
    assert "manifest_path" in payload
    assert "failure_kind" in payload


def test_write_endpoints_blocked_when_dashboard_writes_disabled(tmp_path: Path) -> None:
    paths = _runtime(tmp_path, enable_write=False)
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        # wait for bind
        time.sleep(0.1)
        for endpoint in ("review-gate", "run-tests", "final-gate"):
            with pytest.raises(urllib.error.HTTPError) as exc:
                _post_json(f"http://127.0.0.1:{port}", f"/api/tasks/TASK-x/{endpoint}")
            assert exc.value.code == 403
    finally:
        srv._shutdown_requested = True  # type: ignore[attr-defined]
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def test_files_endpoint_serves_operator_artifacts_but_not_private_runtime_files(writable_dashboard) -> None:
    base, paths = writable_dashboard
    work_item = _create_work_item(base)
    # Issue #194 — `run-tests` is gated on an `apply` artifact now.
    _seed_run_prereqs(paths, work_item["id"])
    _post_json(base, f"/api/tasks/{work_item['id']}/run-tests")
    analysis = paths.runtime_root / "analysis" / work_item["id"]
    analysis.mkdir(parents=True, exist_ok=True)
    (analysis / "requirements.md").write_text("requirements body\n", encoding="utf-8")
    plans = paths.runtime_root / "plans" / work_item["id"]
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "TEST-PLAN.md").write_text("plan body\n", encoding="utf-8")
    runs = paths.runtime_root / "runs" / "run-demo"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "triage.md").write_text("triage body\n", encoding="utf-8")
    runtime_rel = paths.runtime_root.relative_to(paths.repo_root).as_posix()

    with urllib.request.urlopen(base + "/files/reports/summary.md", timeout=3) as resp:
        body = resp.read().decode("utf-8")
        assert resp.status == 200
        assert "fake summary" in body
    with urllib.request.urlopen(
        base + f"/files/{runtime_rel}/analysis/{work_item['id']}/requirements.md",
        timeout=3,
    ) as resp:
        assert resp.status == 200
        assert "requirements body" in resp.read().decode("utf-8")
    with urllib.request.urlopen(
        base + f"/files/{runtime_rel}/plans/{work_item['id']}/TEST-PLAN.md",
        timeout=3,
    ) as resp:
        assert resp.status == 200
        assert "plan body" in resp.read().decode("utf-8")
    with urllib.request.urlopen(base + f"/files/{runtime_rel}/runs/run-demo/triage.md", timeout=3) as resp:
        assert resp.status == 200
        assert "triage body" in resp.read().decode("utf-8")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + f"/files/{runtime_rel}/state.db", timeout=3)
    assert exc.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + "/files/AGENTS.md", timeout=3)
    assert exc.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + "/files/../etc/passwd", timeout=3)
    assert exc.value.code == 404
