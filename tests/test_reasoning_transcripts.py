"""Reasoning transcript capture, redaction, storage, endpoint, CLI, and migration behavior."""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from agentic_os import transcripts as tx
from agentic_os.events import EventLog
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db


class _Env:
    def __init__(self, body="", metadata=None):
        self.body = body
        self.metadata = metadata or {}


class _ExplodingEnv:
    """Envelope whose metadata access raises — proves mode='never' is inert."""
    @property
    def metadata(self):  # pragma: no cover - must never be reached when never
        raise AssertionError("metadata accessed under transcript_capture=never")

    @property
    def body(self):  # pragma: no cover
        raise AssertionError("body accessed under transcript_capture=never")


def _runtime(tmp_path):
    paths = RuntimePaths(repo_root=tmp_path / "repo", runtime_root=tmp_path / "repo" / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    return conn, paths, EventLog(conn, paths)


# ---- migration ----


def test_migration_creates_model_transcripts(tmp_path: Path) -> None:
    conn, _p, _e = _runtime(tmp_path)
    try:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table';"
        ).fetchall()}
        assert "model_transcripts" in names
    finally:
        conn.close()


def test_migration_v9_to_v10(tmp_path: Path) -> None:
    from agentic_os.storage import db as db_mod

    db_path = tmp_path / "legacy.db"
    conn = db_mod.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);
        -- A real v9 DB already has work_items (m2) and autonomy_sessions (m9);
        -- the v14 projects migration ALTERs both, so the fixture must carry them.
        CREATE TABLE work_items (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE autonomy_sessions (id TEXT PRIMARY KEY, started_at TEXT NOT NULL);
        INSERT INTO schema_migrations(version, name, applied_at) VALUES (9, 'pre', '2026-01-01T00:00:00Z');
        """
    )
    assert db_mod.current_version(conn) == 9
    db_mod.migrate(conn)
    # migrate() always advances to the schema head; the v10 step still runs
    # along the way, creating model_transcripts (the behaviour under test).
    assert db_mod.current_version(conn) == db_mod.SCHEMA_VERSION
    assert db_mod.current_version(conn) >= 10
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table';"
    ).fetchall()}
    assert "model_transcripts" in names
    conn.close()


# ---- capture modes ----


def test_never_is_zero_overhead(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        # _ExplodingEnv raises if metadata/body is touched.
        n = tx.capture_transcript(
            conn, events, mode="never", outcome="failed",
            invocation_id="INV1", step_id="s1", envelope=_ExplodingEnv(), stdout_text="x",
        )
        assert n == 0
        rows = conn.execute("SELECT COUNT(*) AS c FROM model_transcripts;").fetchone()
        assert rows["c"] == 0
    finally:
        conn.close()


def test_on_block_skips_ok_captures_failed(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        env = _Env(body="model output text")
        assert tx.capture_transcript(
            conn, events, mode="on_block", outcome="ok",
            invocation_id="INV-ok", step_id="s", envelope=env, stdout_text="",
        ) == 0
        n = tx.capture_transcript(
            conn, events, mode="on_block", outcome="failed",
            invocation_id="INV-fail", step_id="s", envelope=env, stdout_text="",
        )
        assert n == 1
        got = tx.get_transcript(conn, "INV-fail")
        assert got[0]["kind"] == "text"
        assert got[0]["payload"] == "model output text"
    finally:
        conn.close()


def test_always_captures_on_ok(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        env = _Env(body="hello")
        assert tx.capture_transcript(
            conn, events, mode="always", outcome="ok",
            invocation_id="INV-a", step_id="s", envelope=env, stdout_text="",
        ) == 1
    finally:
        conn.close()


def test_structured_metadata_extracted(tmp_path: Path) -> None:
    env = _Env(body="final answer", metadata={
        "thinking": "let me reason",
        "tool_calls": [{"name": "grep", "args": "x"}],
        "tool_results": ["match found"],
    })
    chunks = tx.extract_chunks(env, "")
    kinds = [k for k, _ in chunks]
    assert kinds == ["thinking", "tool_call", "tool_result", "text"]


def test_text_only_fallback(tmp_path: Path) -> None:
    chunks = tx.extract_chunks(_Env(body=""), "raw stdout fallback")
    assert chunks == [("text", "raw stdout fallback")]


# ---- redaction at write ----


def test_redaction_applied_to_row(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        env = _Env(body="here is the api_key=sk-PLANTEDSECRET12345 do not leak")
        tx.capture_transcript(
            conn, events, mode="always", outcome="ok",
            invocation_id="INV-secret", step_id="s", envelope=env, stdout_text="",
        )
        row = conn.execute(
            "SELECT payload FROM model_transcripts WHERE invocation_id='INV-secret';"
        ).fetchone()
        assert "sk-PLANTEDSECRET12345" not in row["payload"]
        assert "<redacted>" in row["payload"]
    finally:
        conn.close()


# ---- correlation event ----


def test_transcript_chunk_event_carries_ids(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        tx.capture_transcript(
            conn, events, mode="always", outcome="ok",
            invocation_id="INV-ev", step_id="STEP-9", envelope=_Env(body="t"), stdout_text="",
        )
        row = conn.execute(
            "SELECT payload FROM events WHERE kind='transcript.chunk';"
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload"])
        assert payload["invocation_id"] == "INV-ev"
        assert payload["step_id"] == "STEP-9"
    finally:
        conn.close()


# ---- config validation ----


def test_config_validates_transcript_capture() -> None:
    from agentic_os.config import _validate

    from test_notification_dispatch import _BASE_CONFIG  # type: ignore
    import copy

    cfg = copy.deepcopy(_BASE_CONFIG)
    cfg["autonomy"] = {"transcript_capture": "on_block"}
    assert _validate(cfg) == []
    cfg["autonomy"]["transcript_capture"] = "sometimes"
    assert any("transcript_capture" in e for e in _validate(cfg))


# ---- endpoint ----


@pytest.fixture
def live(tmp_path):
    conn, paths, events = _runtime(tmp_path)
    tx.capture_transcript(
        conn, events, mode="always", outcome="ok",
        invocation_id="INV-live", step_id="s", envelope=_Env(body="endpoint body"), stdout_text="",
    )
    conn.close()
    from test_dashboard_server import _free_port  # type: ignore

    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"host": "127.0.0.1", "port": port, "paths": paths}
    finally:
        srv.shutdown()
        srv.server_close()


def test_transcript_endpoint(live) -> None:
    with urllib.request.urlopen(
        f"http://{live['host']}:{live['port']}/api/transcripts/INV-live", timeout=5
    ) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    assert payload["invocation_id"] == "INV-live"
    assert payload["chunks"][0]["payload"] == "endpoint body"


# ---- CLI ----


def test_cli_transcripts_show(tmp_path: Path, capsys) -> None:
    import copy

    import yaml  # type: ignore

    from agentic_os.cli import cmd_transcripts
    from agentic_os.orchestrator import open_runtime
    from test_notification_dispatch import _BASE_CONFIG  # type: ignore

    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    cfg = copy.deepcopy(_BASE_CONFIG)
    (repo / "config" / "agentic-os.yml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    conn, paths, events, _o = open_runtime(repo)
    try:
        tx.capture_transcript(
            conn, events, mode="always", outcome="ok",
            invocation_id="INV-cli", step_id="s", envelope=_Env(body="cli body"), stdout_text="",
        )
    finally:
        conn.close()

    rc = cmd_transcripts(repo, ["show", "INV-cli", "--json"], json_output=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["chunks"][0]["payload"] == "cli body"

    rc = cmd_transcripts(repo, ["show", "MISSING"], json_output=False)
    assert rc == 4
