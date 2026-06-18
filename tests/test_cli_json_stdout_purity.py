"""Regression for issue #76 — `--json` stdout must be valid JSON from byte 1.

The human diagnostic banner that names the dispatched command, repo root,
and config path is routed to stderr so any caller piping stdout into `jq`
or a JSON parser succeeds without `sed '1d'` preprocessing.
"""
from __future__ import annotations

import io
import json
import shutil
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agentic_os.cli import main as cli_main


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_json_status_stdout_is_pure_json(tmp_path: Path) -> None:
    rc, stdout, stderr = _run_cli(["--root", str(tmp_path), "--json", "status"])
    assert rc == 0, stderr
    # Must parse cleanly from byte 1 — no banner prefix, no human prose.
    payload = json.loads(stdout)
    assert isinstance(payload, dict)
    assert "runtime" in payload


def test_json_doctor_stdout_is_pure_json(tmp_path: Path) -> None:
    rc, stdout, stderr = _run_cli(["--root", str(tmp_path), "--json", "doctor"])
    # Issue #96 — doctor returns non-zero when there's no config; the
    # important contract here is that stdout still parses as JSON.
    assert rc in (0, 1), stderr
    payload = json.loads(stdout)
    assert isinstance(payload, dict)
    assert "python" in payload
    assert "ok" in payload


def test_human_banner_goes_to_stderr(tmp_path: Path) -> None:
    rc, stdout, stderr = _run_cli(["--root", str(tmp_path), "--json", "status"])
    assert rc == 0
    assert "agentic-os status" in stderr
    assert "agentic-os status" not in stdout


def test_non_json_invocation_also_routes_banner_to_stderr(tmp_path: Path) -> None:
    """Banner is unconditional diagnostic — humans see it on stderr too.

    This pins the contract so future contributors do not reintroduce the
    stdout banner for the human-mode branch.
    """
    rc, stdout, stderr = _run_cli(["--root", str(tmp_path), "status"])
    assert rc == 0
    assert "agentic-os status" in stderr
    assert "agentic-os status" not in stdout


def test_json_status_first_byte_is_open_brace(tmp_path: Path) -> None:
    rc, stdout, _ = _run_cli(["--root", str(tmp_path), "--json", "status"])
    assert rc == 0
    assert stdout.lstrip().startswith("{"), stdout[:80]


def test_json_init_uses_visible_runtime_even_when_legacy_dir_exists(tmp_path: Path) -> None:
    """Regression for PR #132 review.

    When config is absent but a legacy `.agentic-os/` directory already
    exists, `init` must create config first and then bootstrap runtime from
    that config. Otherwise SQLite lands in the legacy root while future
    commands read `agentic-os-runtime/`.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".agentic-os").mkdir()
    config_dir = repo / "config"
    config_dir.mkdir()
    source_example = Path(__file__).resolve().parents[1] / "config" / "agentic-os.yml.example"
    shutil.copyfile(source_example, config_dir / "agentic-os.yml.example")

    rc, stdout, stderr = _run_cli(["--root", str(repo), "--json", "init"])

    assert rc == 0, stderr
    payload = json.loads(stdout)
    assert payload["runtime_root"] == "agentic-os-runtime"
    assert (repo / "agentic-os-runtime" / "state.db").exists()
    assert not (repo / ".agentic-os" / "state.db").exists()
