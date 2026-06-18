from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from agentic_os.generators.api import generate_api_test
from agentic_os.generators.ui import generate_ui_test
from agentic_os.gates import static_review_gate
from agentic_os.paths import RuntimePaths
from agentic_os.plan_v2 import PlanItem
from agentic_os.runtime.subprocess import run_command
from agentic_os.server import make_server


def _api_item(**overrides) -> PlanItem:
    base = dict(
        candidate_id="C-INJECT",
        title="x') }); evilCode(); test('y",
        test_type="api",
        priority="P0",
        decision="generate_now",
        expected_assertion='POST /orders must return HTTP 400 and body.error.code = "bad`value"',
        source_refs=["docs/openapi.yaml#/paths/~1orders/post"],
        target_method="POST",
        target_path="/orders');evilCode();ctx.get('/safe",
        required_test_data='{"note":"` ${process.env.SECRET_TOKEN} `"}',
        cleanup_strategy="DELETE /orders/123');evilCode();ctx.get('/safe",
        generator_target="playwright-ts",
    )
    base.update(overrides)
    return PlanItem(**base)


def _ui_item(**overrides) -> PlanItem:
    base = dict(
        candidate_id="UI-INJECT",
        title="x`); evilCode(); (`y",
        test_type="ui",
        priority="P0",
        decision="generate_now",
        expected_assertion='URL must contain /orders/?id=1&ok=true and text "Order `created`; evilCode();" must be visible',
        source_refs=["docs/ui.md#orders"],
        target_page="/orders/new`);evilCode();(`",
        generator_target="playwright-ts",
        notes=[],
    )
    base.update(overrides)
    return PlanItem(**base)


def _node_check(content: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for generated TypeScript parse checks")
    spec = tmp_path / "generated.spec.ts"
    spec.write_text(content, encoding="utf-8")
    subprocess.run([node, "--check", str(spec)], check=True)


def test_api_generator_escapes_plan_fields_and_parses_with_node(tmp_path: Path) -> None:
    spec = generate_api_test(_api_item()).content

    _node_check(spec, tmp_path)
    assert "JSON.parse(`" not in spec
    assert "ctx.post('/orders" not in spec
    assert "ctx.delete('/orders" not in spec
    assert "test('C-INJECT" not in spec
    assert 'process.env["SECRET_TOKEN"]' not in spec


def test_ui_generator_escapes_target_page_and_parses_with_node(tmp_path: Path) -> None:
    spec = generate_ui_test(_ui_item()).content

    _node_check(spec, tmp_path)
    assert "page.goto(`${UI_BASE_URL}" not in spec
    assert 'const targetPage = "/orders/new`);evilCode();(`";' in spec
    assert "test('UI-INJECT" not in spec


def test_subprocess_redacts_inherited_secret_env_in_logs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-parent-secret-should-not-leak")
    log_path = tmp_path / "parent-env.log"

    # The fallback sentinel must be a token that does NOT appear in the logged
    # command argv, otherwise `in log` matches the [status] line regardless of
    # what the child actually printed (a false-pass — issue #291 review).
    result = run_command(
        [
            sys.executable,
            "-c",
            "import os; print('KEY_IS_' + os.getenv('ANTHROPIC_API_KEY', 'ABSENT'))",
        ],
        cwd=tmp_path,
        log_path=log_path,
        timeout_seconds=5,
    )

    assert result.exit_code == 0
    log = log_path.read_text(encoding="utf-8")
    # The default path forwards provider credentials, so the child sees the key
    # and its value is redacted in the log; the raw secret never appears.
    assert "sk-parent-secret-should-not-leak" not in log
    assert "[REDACTED:secret_in_env]" in log


def test_child_env_forwards_provider_credentials_by_default(monkeypatch) -> None:
    """Issue #291 — model CLIs (default path) keep provider credentials."""
    from agentic_os.runtime.subprocess import _build_child_env

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-model-needs-this")
    env = _build_child_env()
    assert env.get("ANTHROPIC_API_KEY") == "sk-model-needs-this"


def test_child_env_drops_provider_credentials_for_sut(monkeypatch) -> None:
    """Issue #291 — SUT commands launch without operator model keys."""
    from agentic_os.runtime.subprocess import _build_child_env

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-sut-must-not-see")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-sut-must-not-see-2")
    env = _build_child_env(include_provider_credentials=False)
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    # Core process env still flows so the SUT binary can run.
    assert "PATH" in env


def test_scrub_provider_credentials_strips_explicit_env(monkeypatch) -> None:
    """Issue #291 — an explicit os.environ copy must lose model keys."""
    from agentic_os.runtime.subprocess import scrub_provider_credentials

    source = {
        "PATH": "/usr/bin",
        "API_BASE_URL": "http://localhost:8000",
        "ANTHROPIC_API_KEY": "sk-leak",
        "GEMINI_API_KEY": "gk-leak",
    }
    scrubbed = scrub_provider_credentials(source)
    assert scrubbed == {"PATH": "/usr/bin", "API_BASE_URL": "http://localhost:8000"}
    # Original mapping is untouched.
    assert "ANTHROPIC_API_KEY" in source


def test_subprocess_redacts_explicit_secret_env_values(tmp_path: Path) -> None:
    log_path = tmp_path / "explicit-env.log"

    result = run_command(
        [sys.executable, "-c", "import os; print('SECRET=' + os.environ['SECRET'])"],
        cwd=tmp_path,
        log_path=log_path,
        timeout_seconds=5,
        env={"SECRET": "sk-x"},
    )

    assert result.exit_code == 0
    log = log_path.read_text(encoding="utf-8")
    assert "[REDACTED:secret_in_env]" in log
    assert "sk-x" not in log


def test_dashboard_redacts_historical_subprocess_logs_on_read(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / "agentic-os-runtime")
    paths.ensure()
    log_path = paths.subprocess_logs_dir / "historical.log"
    log_path.write_text(
        "Authorization: Bearer sk-live-secret-value\npassword=letmein\n",
        encoding="utf-8",
    )
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        time.sleep(0.1)
        runtime_rel = paths.runtime_root.relative_to(paths.repo_root).as_posix()
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/files/{runtime_rel}/logs/subprocess/historical.log",
            timeout=3,
        ) as resp:
            body = resp.read().decode("utf-8")
    finally:
        srv._shutdown_requested = True  # type: ignore[attr-defined]
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)

    assert resp.status == 200
    assert "sk-live-secret-value" not in body
    assert "letmein" not in body
    assert "[REDACTED:" in body


def test_static_gate_rejects_raw_generator_js_interpolation() -> None:
    diff = (
        "diff --git a/scripts/agentic-os/agentic_os/generators/api.py "
        "b/scripts/agentic-os/agentic_os/generators/api.py\n"
        "+++ b/scripts/agentic-os/agentic_os/generators/api.py\n"
        "@@ -1 +1 @@\n"
        "+return f\"test('{item.candidate_id}', async () => {})\"\n"
    )

    gate = static_review_gate(diff, scope="api")

    assert gate.verdict == "REJECT"
    assert gate.reason == "generator_interpolation"


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
