"""Support-bundle builder + CLI + dashboard endpoint tests (issue #146)."""
from __future__ import annotations

import io
import json
import sys
import tarfile
import threading
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from agentic_os.cli import cmd_support_bundle, main as cli_main
from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db
from agentic_os.support_bundle import (
    SUPPORT_BUNDLE_DIRNAME,
    SUPPORT_BUNDLE_SUBSYSTEMS,
    build_support_bundle,
    redact_config,
)
from tests.test_dashboard_task_ui import _DEFAULT_CONFIG, _free_port, _wait


def _runtime(tmp_path: Path, *, enable_write: bool = False) -> RuntimePaths:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    cfg = repo / ".qualitycat" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_DEFAULT_CONFIG.format(write=str(enable_write).lower()).lstrip(), encoding="utf-8")
    conn = init_db(paths.db)
    Orchestrator(conn, paths, EventLog(conn, paths)).seed_phases()
    conn.close()
    return paths


def test_redact_config_replaces_secret_shaped_leaves() -> None:
    src = {
        "models": {
            "planner": {"command": ["claude"], "api_key": "sk-12345"},
            "implementer": {"token": "abc", "role": "sonnet"},
        },
        "auth": {
            "PASSWORD": "hunter2",
            "client_secret": "xyz",
            "user": "ops@example.com",  # not secret-shaped — preserved
        },
        "providers": [{"BearerToken": "tok"}, {"label": "ok"}],
        "credential": "literal-value-by-key-name",
    }
    out = redact_config(src)
    assert out["models"]["planner"]["api_key"] == "<redacted>"
    assert out["models"]["planner"]["command"] == ["claude"]  # preserved
    assert out["models"]["implementer"]["token"] == "<redacted>"
    assert out["models"]["implementer"]["role"] == "sonnet"
    assert out["auth"]["PASSWORD"] == "<redacted>"
    assert out["auth"]["client_secret"] == "<redacted>"
    assert out["auth"]["user"] == "ops@example.com"
    assert out["providers"][0]["BearerToken"] == "<redacted>"
    assert out["providers"][1]["label"] == "ok"
    # `credential` as a top-level leaf key also redacts (denylist matches `credential`).
    assert out["credential"] == "<redacted>"


def test_redact_config_does_not_mutate_input() -> None:
    src = {"api_key": "secret"}
    redact_config(src)
    assert src["api_key"] == "secret", "redact must not mutate the input dict"


def test_build_support_bundle_writes_tarball_with_expected_members(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    repo = paths.repo_root
    # Seed an event log file and a fake run + bug so the bundle gathers them.
    (paths.events_dir / "ingest.jsonl").write_text(
        "\n".join(json.dumps({"type": "test", "i": i}) for i in range(5)) + "\n",
        encoding="utf-8",
    )
    runs = paths.runtime_root / "runs" / "RUN-001"
    runs.mkdir(parents=True)
    (runs / "manifest.json").write_text(json.dumps({"run_id": "RUN-001"}), encoding="utf-8")
    (runs / "report.txt").write_text("PASS\n", encoding="utf-8")
    bugs = repo / "bugs"
    bugs.mkdir()
    (bugs / "BUG-001.json").write_text(json.dumps({"id": "BUG-001"}), encoding="utf-8")

    result = build_support_bundle(repo, paths)

    bundle_path = repo / result["path"]
    assert bundle_path.exists()
    assert bundle_path.suffix == ".gz"
    assert result["bytes"] == bundle_path.stat().st_size
    assert result["download_url"] if "download_url" in result else True  # set by server, not by builder
    assert SUPPORT_BUNDLE_DIRNAME in str(bundle_path)

    with tarfile.open(bundle_path, "r:gz") as tar:
        members = sorted(tar.getnames())
    # Required members in every bundle.
    assert "MANIFEST.json" in members
    assert "doctor.json" in members
    # Config got bundled (PyYAML is available in CI; the legacy config path was seeded).
    assert any(name.startswith("config/") for name in members)
    assert "events/ingest.jsonl" in members
    assert "runs/RUN-001/manifest.json" in members
    assert "runs/RUN-001/report.txt" in members
    assert "bugs/BUG-001.json" in members

    manifest = result["manifest"]
    by_arc = {f["arcname"]: f for f in manifest["files"]}
    assert by_arc["doctor.json"]["bytes_in_bundle"] > 0
    assert by_arc["events/ingest.jsonl"]["bytes_in_bundle"] > 0


def test_build_support_bundle_redacts_config_inside_tarball(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    # Append a secret-shaped key to the seeded config so we can prove it got redacted.
    cfg_path = paths.repo_root / ".qualitycat" / "agentic-os.yml"
    cfg_path.write_text(cfg_path.read_text() + "\nauth:\n  api_key: sk-LEAKED-SECRET\n", encoding="utf-8")

    result = build_support_bundle(paths.repo_root, paths)
    bundle_path = paths.repo_root / result["path"]

    with tarfile.open(bundle_path, "r:gz") as tar:
        # Either canonical or legacy config got bundled — find whichever exists.
        members = [m for m in tar.getmembers() if m.name.startswith("config/") and m.name.endswith(".yml")]
        assert members, "config file should be embedded"
        body = tar.extractfile(members[0]).read().decode("utf-8")

    assert "sk-LEAKED-SECRET" not in body
    assert "<redacted>" in body


def test_build_support_bundle_truncates_oversize_files(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    runs = paths.runtime_root / "runs" / "RUN-BIG"
    runs.mkdir(parents=True)
    big = runs / "huge.txt"
    big.write_bytes(b"A" * (300 * 1024))  # > 256 KiB cap

    result = build_support_bundle(paths.repo_root, paths)
    manifest = result["manifest"]
    entry = next(
        f for f in manifest["files"]
        if f["arcname"] == "runs/RUN-BIG/huge.txt"
    )
    assert entry["truncated"] is True
    assert entry["original_bytes"] == 300 * 1024
    assert entry["bytes_in_bundle"] <= 256 * 1024


def test_cli_support_bundle_returns_json_payload(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main([
            "--root",
            str(paths.repo_root),
            "--json",
            "support-bundle",
        ])
    assert rc == 0, err.getvalue()
    payload = json.loads(out.getvalue())
    assert payload["bytes"] > 0
    assert payload["filename"].startswith("support-")
    assert payload["filename"].endswith(".tar.gz")
    bundle_path = paths.repo_root / payload["path"]
    assert bundle_path.exists()


def test_dashboard_support_bundle_endpoint_builds_and_serves(tmp_path: Path) -> None:
    paths = _runtime(tmp_path, enable_write=True)
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        _wait(base + "/healthz", timeout=5).read()

        req = urllib.request.Request(
            base + "/api/support-bundle",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["bytes"] > 0
        assert payload["download_url"].startswith("/files/")
        assert "disclaimer" in payload

        # Tarball must be reachable via the static-file route.
        with urllib.request.urlopen(base + payload["download_url"], timeout=5) as dl:
            data = dl.read()
            assert dl.headers.get("Content-Type", "").startswith("application/gzip")
        assert len(data) == payload["bytes"]
    finally:
        srv._shutdown_requested = True
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _seed_full_bundle_inputs(paths: RuntimePaths) -> None:
    """Seed events + runs + bugs so every subsystem has something to gather."""
    (paths.events_dir / "ingest.jsonl").write_text(
        json.dumps({"type": "test"}) + "\n", encoding="utf-8"
    )
    runs = paths.runtime_root / "runs" / "RUN-001"
    runs.mkdir(parents=True)
    (runs / "manifest.json").write_text(json.dumps({"run_id": "RUN-001"}), encoding="utf-8")
    bugs = paths.repo_root / "bugs"
    bugs.mkdir()
    (bugs / "BUG-001.json").write_text(json.dumps({"id": "BUG-001"}), encoding="utf-8")


def test_subsystem_set_exposes_expected_names() -> None:
    assert SUPPORT_BUNDLE_SUBSYSTEMS == frozenset(
        {"config", "doctor", "events", "runs", "bugs"}
    )


def test_build_support_bundle_include_restricts_subsystems(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    _seed_full_bundle_inputs(paths)

    result = build_support_bundle(paths.repo_root, paths, include={"doctor"})

    bundle_path = paths.repo_root / result["path"]
    with tarfile.open(bundle_path, "r:gz") as tar:
        members = set(tar.getnames())
    # MANIFEST.json is the manifest itself, always present.
    assert "MANIFEST.json" in members
    assert "doctor.json" in members
    # Excluded subsystems leave no members behind.
    assert not any(m.startswith("events/") for m in members)
    assert not any(m.startswith("runs/") for m in members)
    assert not any(m.startswith("bugs/") for m in members)
    assert not any(m.startswith("config/") for m in members)
    assert result["manifest"]["subsystems_enabled"] == ["doctor"]


def test_build_support_bundle_exclude_drops_subsystems(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    _seed_full_bundle_inputs(paths)

    result = build_support_bundle(paths.repo_root, paths, exclude={"events", "bugs"})

    bundle_path = paths.repo_root / result["path"]
    with tarfile.open(bundle_path, "r:gz") as tar:
        members = set(tar.getnames())
    assert "doctor.json" in members
    assert any(m.startswith("config/") for m in members)
    assert any(m.startswith("runs/") for m in members)
    assert not any(m.startswith("events/") for m in members)
    assert not any(m.startswith("bugs/") for m in members)
    assert set(result["manifest"]["subsystems_enabled"]) == {"config", "doctor", "runs"}


def test_build_support_bundle_include_exclude_mutually_exclusive(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    with pytest.raises(ValueError, match="mutually exclusive"):
        build_support_bundle(paths.repo_root, paths, include={"doctor"}, exclude={"bugs"})


def test_build_support_bundle_rejects_unknown_subsystem(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    with pytest.raises(ValueError, match="unknown subsystem"):
        build_support_bundle(paths.repo_root, paths, include={"telemetry"})


def test_build_support_bundle_no_redact_embeds_verbatim(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    cfg_path = paths.repo_root / ".qualitycat" / "agentic-os.yml"
    cfg_path.write_text(
        cfg_path.read_text() + "\nauth:\n  api_key: sk-VERBATIM-VALUE\n",
        encoding="utf-8",
    )

    result = build_support_bundle(paths.repo_root, paths, redact=False)
    bundle_path = paths.repo_root / result["path"]

    with tarfile.open(bundle_path, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name.endswith("agentic-os.yml")]
        body = tar.extractfile(members[0]).read().decode("utf-8")

    assert "sk-VERBATIM-VALUE" in body
    assert "<redacted>" not in body
    assert result["manifest"]["redacted"] is False
    assert "REDACTION DISABLED" in result["disclaimer"]


def test_build_support_bundle_dest_overrides_output_dir(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    alt = tmp_path / "alt-output"
    # `dest` does not need to exist beforehand; the builder creates it.
    result = build_support_bundle(paths.repo_root, paths, dest=alt)

    bundle_path = Path(result["absolute_path"])
    assert bundle_path.exists()
    assert bundle_path.parent == alt
    # `dest` lives outside repo_root in this test, so `path` falls back to absolute.
    assert result["path"] == str(bundle_path)


def test_build_support_bundle_tag_appears_in_filename(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    result = build_support_bundle(paths.repo_root, paths, tag="ticket-42")

    assert result["filename"].endswith("-ticket-42.tar.gz")
    assert result["manifest"]["tag"] == "ticket-42"


def test_build_support_bundle_tag_rejects_unsafe_chars(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    with pytest.raises(ValueError, match="tag"):
        build_support_bundle(paths.repo_root, paths, tag="../escape")


def test_cli_support_bundle_accepts_new_flags(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    _seed_full_bundle_inputs(paths)
    alt = tmp_path / "operator-dest"
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main([
            "--root", str(paths.repo_root),
            "--json",
            "support-bundle",
            "--include", "doctor,events",
            "--dest", str(alt),
            "--tag", "smoke",
        ])
    assert rc == 0, err.getvalue()
    payload = json.loads(out.getvalue())
    assert payload["filename"].endswith("-smoke.tar.gz")
    assert payload["manifest"]["subsystems_enabled"] == ["doctor", "events"]
    bundle_path = Path(payload["absolute_path"])
    assert bundle_path.exists()
    assert bundle_path.parent == alt


def test_cli_support_bundle_rejects_both_include_and_exclude(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main([
            "--root", str(paths.repo_root),
            "support-bundle",
            "--include", "doctor",
            "--exclude", "bugs",
        ])
    assert rc == 64, err.getvalue()
    assert "mutually exclusive" in err.getvalue()


def test_dashboard_support_bundle_endpoint_refuses_when_write_disabled(tmp_path: Path) -> None:
    paths = _runtime(tmp_path, enable_write=False)
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        _wait(base + "/healthz", timeout=5).read()

        req = urllib.request.Request(
            base + "/api/support-bundle",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 403
        body = json.loads(exc.value.read().decode("utf-8"))
        assert body["error"] == "dashboard_write_disabled"
    finally:
        srv._shutdown_requested = True
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)
