#!/usr/bin/env python3
"""RC proof orchestrator for the fake-SUT fixture (issue #137).

End-to-end smoke that exercises the deterministic half of the Agentic
OS pipeline without any model call or network access:

    init  →  inbox synthesise  →  task analyse  →  task plan
          →  run dry-run --fake-sut  →  assert artifacts

The proof intentionally stops before ``task implement-tests`` because
that step depends on a real LLM. The online half (running generated
tests against ``server.py``) is documented in ``README.md`` and stays
out of this automated proof.

Usage:
    python examples/fake-sut/run-rc-proof.py [WORKSPACE]

If WORKSPACE is omitted, a temp directory is created and cleaned up on
success. On failure, the workspace is left in place so the operator
can inspect ``.agentic-os-runtime/`` and the reports.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
AGENTIC_OS_SRC = REPO_ROOT / "scripts" / "agentic-os"


def _run_cli(workspace: Path, args: List[str], *, capture: bool = False) -> str:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{AGENTIC_OS_SRC}{os.pathsep}{existing}" if existing else str(AGENTIC_OS_SRC)
    )
    # `--json` is a GLOBAL flag on the agentic-os CLI; it must precede
    # the subcommand name. Subcommand parsers reject it as unknown.
    # `-m agentic_os` (the package) runs __main__.py which forwards to
    # cli.main; `-m agentic_os.cli` only imports the module (no
    # __main__ guard) and silently exits 0.
    cmd = [sys.executable, "-m", "agentic_os", "--json", *args]
    proc = subprocess.run(
        cmd, cwd=workspace, env=env, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        sys.stderr.write(f"\nCLI exited {proc.returncode}: {' '.join(cmd)}\n")
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit(proc.returncode)
    return proc.stdout if capture else ""


def _seed_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HERE / "openapi.yaml", workspace / "openapi.yaml")
    pretask_dir = workspace / "pretask"
    pretask_dir.mkdir(exist_ok=True)
    shutil.copy2(HERE / "pretask.md", pretask_dir / "pretask.md")
    # `agentic-os init` copies a config template into the workspace; it
    # refuses with `InfraError` if the template is missing, so we seed
    # it from the repo before running init.
    template_src = REPO_ROOT / "config" / "agentic-os.yml.example"
    if not template_src.exists():
        raise SystemExit(f"missing config template at {template_src}")
    template_dst = workspace / "config" / "agentic-os.yml.example"
    template_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template_src, template_dst)


def _patch_config_for_openapi(workspace: Path) -> None:
    try:
        import yaml
    except ImportError:
        sys.stderr.write(
            "PyYAML is required to patch the config; install with `pip install pyyaml`.\n"
        )
        raise SystemExit(2)
    cfg_path = workspace / "config" / "agentic-os.yml"
    if not cfg_path.exists():
        legacy = workspace / ".qualitycat" / "agentic-os.yml"
        if legacy.exists():
            cfg_path = legacy
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    sut = cfg.setdefault("sut", {})
    openapi = sut.setdefault("openapi", {})
    openapi["sources"] = [{"type": "file", "value": "openapi.yaml"}]
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def _runtime_root(workspace: Path) -> Path:
    """Locate the runtime root the CLI actually used.

    `agentic-os init` writes to ``agentic-os-runtime/`` by default, but
    older configs may still target ``.agentic-os/``. The proof reads
    artifacts from whichever exists, biased to the visible directory.
    """
    visible = workspace / "agentic-os-runtime"
    legacy = workspace / ".agentic-os"
    if visible.exists():
        return visible
    if legacy.exists():
        return legacy
    raise SystemExit(f"no runtime root found under {workspace}")


def _check(condition: bool, message: str, problems: List[str]) -> None:
    if not condition:
        problems.append(message)


def _validate_artifacts(workspace: Path, work_item_id: str) -> List[str]:
    runtime = _runtime_root(workspace)
    problems: List[str] = []

    sut_map = runtime / "analysis" / work_item_id / "sut-map.json"
    _check(sut_map.exists(), f"missing {sut_map.relative_to(workspace)}", problems)
    if sut_map.exists():
        data = json.loads(sut_map.read_text(encoding="utf-8"))
        ops: List[dict] = []
        for inv in data.get("openapi_inventory") or []:
            ops.extend(inv.get("operations") or [])
        _check(
            len(ops) >= 4,
            f"sut-map.json reports {len(ops)} OpenAPI operations (expected ≥4)",
            problems,
        )

    candidates = runtime / "analysis" / work_item_id / "candidate-tests.json"
    _check(
        candidates.exists(),
        f"missing {candidates.relative_to(workspace)}",
        problems,
    )
    if candidates.exists():
        data = json.loads(candidates.read_text(encoding="utf-8"))
        items = data.get("candidates") or data.get("items") or []
        _check(
            len(items) >= 3,
            f"candidate-tests.json has {len(items)} candidates (expected ≥3)",
            problems,
        )

    test_plan = runtime / "plans" / work_item_id / "TEST-PLAN.json"
    _check(test_plan.exists(), f"missing {test_plan.relative_to(workspace)}", problems)
    if test_plan.exists():
        data = json.loads(test_plan.read_text(encoding="utf-8"))
        _check(
            len(data.get("items") or []) >= 1,
            f"TEST-PLAN.json has no items",
            problems,
        )

    last_run = workspace / "reports" / "last-run.json"
    _check(last_run.exists(), f"missing {last_run.relative_to(workspace)}", problems)
    if last_run.exists():
        data = json.loads(last_run.read_text(encoding="utf-8"))
        _check(
            bool(data.get("discovery_only")),
            "reports/last-run.json missing discovery_only flag",
            problems,
        )
    return problems


def main() -> int:
    explicit_workspace = len(sys.argv) > 1
    if explicit_workspace:
        workspace = Path(sys.argv[1]).resolve()
    else:
        workspace = Path(tempfile.mkdtemp(prefix="agentic-rc-proof-")).resolve()

    print(f">>> workspace: {workspace}")
    passed = False
    try:
        _seed_workspace(workspace)
        _run_cli(workspace, ["init", "--force"])
        _patch_config_for_openapi(workspace)

        synth_out = _run_cli(workspace, ["inbox", "synthesize"], capture=True)
        synth = json.loads(synth_out)
        if synth.get("status") != "created":
            sys.stderr.write(f"inbox synthesize did not create a work item: {synth}\n")
            return 1
        work_item_id = synth["work_item_id"]
        print(f">>> work_item_id: {work_item_id}")

        _run_cli(workspace, ["task", "analyze", work_item_id])
        _run_cli(workspace, ["task", "plan", work_item_id])
        _run_cli(workspace, ["run", "dry-run", "--fake-sut"])

        problems = _validate_artifacts(workspace, work_item_id)
        if problems:
            print("RC PROOF: FAIL")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("RC PROOF: PASS")
        passed = True
        return 0
    finally:
        if not explicit_workspace and passed:
            shutil.rmtree(workspace, ignore_errors=True)
        elif not passed:
            sys.stderr.write(f"workspace kept for inspection: {workspace}\n")


if __name__ == "__main__":
    rc = main()
    raise SystemExit(rc)
