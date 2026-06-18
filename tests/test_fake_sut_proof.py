"""Smoke test for the RC proof fixture (issue #137).

Runs `examples/fake-sut/run-rc-proof.py` in a temp workspace and
asserts it exits 0. Walks the full deterministic pipeline (init →
inbox synthesise → task analyse → task plan → run dry-run --fake-sut)
under one second.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROOF_SCRIPT = REPO_ROOT / "examples" / "fake-sut" / "run-rc-proof.py"


def test_rc_proof_script_passes_on_temp_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "rc-proof-ws"
    env = os.environ.copy()
    # The script auto-imports PyYAML when patching the seeded config.
    # The repo's `.venv` already has it; this test inherits that env.
    proc = subprocess.run(
        [sys.executable, str(PROOF_SCRIPT), str(workspace)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, (
        f"RC proof failed (exit {proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "RC PROOF: PASS" in proc.stdout
    # Workspace was passed explicitly, so the script leaves it on disk
    # for inspection — pytest's tmp_path cleanup handles removal.
    assert (workspace / "agentic-os-runtime").exists()
    assert (workspace / "reports" / "last-run.json").exists()
