"""Regression for issue #183: run-tests.sh default path on macOS Bash 3.2.

The mandatory runner wrapper expands an empty `marker_args=()` array under
`set -u`, which raises `unbound variable` on Bash 3.2 (macOS default).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _build_sandbox(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Copy run-tests.sh into a sandboxed project root with stubs.

    The sandbox lets us execute the runner end-to-end without polluting
    the real repo's build/ tree and without invoking pytest recursively.
    """

    sandbox = tmp_path / "proj"
    sandbox.mkdir()

    shutil.copy(REPO_ROOT / "run-tests.sh", sandbox / "run-tests.sh")
    _make_executable(sandbox / "run-tests.sh")

    (sandbox / "tests").mkdir()

    scripts = sandbox / "scripts"
    scripts.mkdir()
    stub_bodies = {
        "copy-reports.sh": "#!/usr/bin/env bash\nmkdir -p reports\nexit 0\n",
        "extract-last-run.sh": (
            "#!/usr/bin/env bash\n"
            "mkdir -p reports\n"
            'printf "%s" \'{"total":0,"passed":0,"failed":0,"skipped":0,'
            '"discovery_only":true}\' > reports/last-run.json\n'
            "exit 0\n"
        ),
        "build-summary.sh": (
            "#!/usr/bin/env bash\n"
            "mkdir -p reports\n"
            'printf "stub summary\\n" > reports/summary.md\n'
            "exit 0\n"
        ),
    }
    for name, body in stub_bodies.items():
        stub = scripts / name
        stub.write_text(body, encoding="utf-8")
        _make_executable(stub)

    # Stub python3 used by the runner. It must accept the pytest invocation
    # exactly as run-tests.sh formats it and exit 0 without doing any work.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_py = bin_dir / "python3"
    stub_py.write_text(
        "#!/usr/bin/env bash\n"
        "# stub: record args, write a synthetic junit, exit 0\n"
        'junit=""\n'
        'while (( $# )); do\n'
        '  case "$1" in\n'
        '    --junitxml) junit="$2"; shift 2 ;;\n'
        '    --junitxml=*) junit="${1#--junitxml=}"; shift ;;\n'
        '    *) shift ;;\n'
        '  esac\n'
        'done\n'
        'if [[ -n "$junit" ]]; then\n'
        '  mkdir -p "$(dirname "$junit")"\n'
        '  printf "%s\\n" "<testsuite name=stub tests=\\"0\\"/>" > "$junit"\n'
        'fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    _make_executable(stub_py)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PROJECT_ROOT": str(sandbox),
        "PYTHON": "python3",
    }
    return sandbox, env


@pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available on this platform"
)
def test_default_runner_does_not_break_on_empty_marker_args(tmp_path: Path) -> None:
    """Default invocation (no --browser) must not raise `unbound variable`.

    Reproduces the failure reported in issue #183 — on macOS Bash 3.2 the
    runner exits at line 103 because `"${marker_args[@]}"` is expanded
    while the array is empty under `set -u`.
    """

    sandbox, env = _build_sandbox(tmp_path)
    result = subprocess.run(
        ["bash", str(sandbox / "run-tests.sh")],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(sandbox),
    )

    assert "unbound variable" not in result.stderr, (
        f"empty array expansion regressed: stderr={result.stderr!r}"
    )
    assert result.returncode == 0, (
        f"runner failed with rc={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


@pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available on this platform"
)
def test_default_runner_invokes_pytest(tmp_path: Path) -> None:
    """The default path must reach the pytest invocation, not bail early."""

    sandbox, env = _build_sandbox(tmp_path)
    result = subprocess.run(
        ["bash", str(sandbox / "run-tests.sh")],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(sandbox),
    )

    junit = sandbox / "build" / "test-results" / "test" / "agentic-os-pytest.xml"
    assert junit.is_file(), (
        f"runner did not reach pytest (no junit produced)\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
