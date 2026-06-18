"""Inbox ingest pipeline — parse documents into task specs, archive on success,
quarantine on failure."""
from __future__ import annotations

import base64
import io
import json
import sqlite3
import threading
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from agentic_os.events import EventLog
from agentic_os.inbox import (
    INBOX_DIRNAME,
    ARCHIVE_DIRNAME,
    EXTRACTION_STATUS_FAILED,
    EXTRACTION_STATUS_LOW,
    EXTRACTION_STATUS_OK,
    FAILED_DIRNAME,
    PRETASK_DIRNAME,
    SUPPORTED_EXTS,
    classify_intake_file,
    ingest_inbox,
    list_inbox_files,
    synthesize_inbox_task,
)
from agentic_os.cli import main as cli_main
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db
from agentic_os.storage.db import connect
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
    (repo / INBOX_DIRNAME).mkdir(parents=True, exist_ok=True)
    return paths


def test_ingest_markdown_creates_task_and_archives(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    src = paths.repo_root / INBOX_DIRNAME / "checkout-flow.md"
    src.write_text(
        "# Checkout flow regression\n\nPriority: P1\nSUT root: .\n\n"
        "## Expected behavior\nCart must not lose items after refresh.\n",
        encoding="utf-8",
    )
    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        results = ingest_inbox(conn, paths, events)
        assert len(results) == 1
        r = results[0]
        assert r["status"] == "created"
        assert r["title"] == "Checkout flow regression"
        assert r["work_item_id"].startswith("TASK-")
        assert r["archived_to"].startswith(f"{INBOX_DIRNAME}/{ARCHIVE_DIRNAME}/")
        # Source moved; archive copy carries the original ext.
        assert not src.exists()
        archive_dir = paths.repo_root / INBOX_DIRNAME / ARCHIVE_DIRNAME
        archived = list(archive_dir.iterdir())
        assert len(archived) == 1
        assert archived[0].suffix == ".md"
        # Task spec persisted with the original H1 title.
        spec_files = list(paths.task_specs_dir.glob(f"{r['work_item_id']}.md"))
        assert len(spec_files) == 1
        body = spec_files[0].read_text(encoding="utf-8")
        assert "# Checkout flow regression" in body
        assert "Cart must not lose items" in body
    finally:
        conn.close()


def test_ingest_plaintext_wraps_into_spec_template(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    src = paths.repo_root / INBOX_DIRNAME / "note.txt"
    src.write_text(
        "Order rejection 422\n\nPOST /orders must return 422 with field list when\n"
        "payload is invalid.\n",
        encoding="utf-8",
    )
    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        results = ingest_inbox(conn, paths, events)
        assert len(results) == 1
        r = results[0]
        assert r["status"] == "created"
        assert r["title"] == "Order rejection 422"
        spec = (paths.task_specs_dir / f"{r['work_item_id']}.md").read_text(encoding="utf-8")
        assert "## Expected behavior" in spec
        assert "POST /orders must return 422" in spec
    finally:
        conn.close()


def test_ingest_unsupported_extension_moves_to_failed(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    src = paths.repo_root / INBOX_DIRNAME / "binary.bin"
    src.write_bytes(b"not a doc")
    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        results = ingest_inbox(conn, paths, events)
        assert len(results) == 1
        r = results[0]
        assert r["status"] == "failed"
        assert "unsupported extension" in r["error"]
        assert r["archived_to"].startswith(f"{INBOX_DIRNAME}/{FAILED_DIRNAME}/")
        failed_dir = paths.repo_root / INBOX_DIRNAME / FAILED_DIRNAME
        sidecars = list(failed_dir.glob("*.error.txt"))
        assert len(sidecars) == 1
        assert "unsupported extension" in sidecars[0].read_text(encoding="utf-8")
    finally:
        conn.close()


def test_ingest_empty_markdown_fails(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    src = paths.repo_root / INBOX_DIRNAME / "empty.md"
    src.write_bytes(b" \n  \n")  # nonzero bytes, but only whitespace
    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        results = ingest_inbox(conn, paths, events)
        assert len(results) == 1
        assert results[0]["status"] == "failed"
        assert "empty" in results[0]["error"]
    finally:
        conn.close()


def test_list_inbox_files_skips_archive_and_hidden(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    inbox = paths.repo_root / INBOX_DIRNAME
    (inbox / ".archive").mkdir()
    (inbox / ".archive" / "old.md").write_text("# old\n", encoding="utf-8")
    (inbox / ".failed").mkdir()
    (inbox / ".failed" / "bad.txt").write_text("nope\n", encoding="utf-8")
    (inbox / ".hidden.md").write_text("# hidden\n", encoding="utf-8")
    (inbox / "real.md").write_text("# real\n", encoding="utf-8")
    files = list_inbox_files(paths)
    assert [f.name for f in files] == ["real.md"]


def test_list_inbox_files_skips_readme_placeholder(tmp_path: Path) -> None:
    """README.md is a tracked operator-facing placeholder at intake roots.
    Listing must skip it (case-insensitively) so neither ingest nor
    synthesize materializes it into a junk task spec."""
    paths = _runtime(tmp_path)
    inbox = paths.repo_root / INBOX_DIRNAME
    pretask = paths.repo_root / PRETASK_DIRNAME
    pretask.mkdir()
    (inbox / "README.md").write_text("# Inbox\n\nDrop docs here.\n", encoding="utf-8")
    (pretask / "readme.md").write_text("# Pretask\n", encoding="utf-8")
    (inbox / "real.md").write_text("# Real task\n", encoding="utf-8")

    files = list_inbox_files(paths)

    assert [str(f.relative_to(paths.repo_root)) for f in files] == [
        f"{INBOX_DIRNAME}/real.md"
    ]


def test_ingest_inbox_preserves_readme_placeholder(tmp_path: Path) -> None:
    """End-to-end: README.md placeholder survives an ingest pass, real
    documents still flow through."""
    paths = _runtime(tmp_path)
    inbox = paths.repo_root / INBOX_DIRNAME
    (inbox / "README.md").write_text(
        "# Inbox\n\nDrop docs here.\n", encoding="utf-8"
    )
    (inbox / "real.md").write_text(
        "# Real task\n\nPriority: P2\nSUT root: .\n\n## Expected behavior\nWorks.\n",
        encoding="utf-8",
    )
    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        results = ingest_inbox(conn, paths, events)
        assert [r["source"] for r in results] == [f"{INBOX_DIRNAME}/real.md"]
        assert (inbox / "README.md").exists()
        assert not (inbox / "real.md").exists()
    finally:
        conn.close()


def test_list_inbox_files_includes_pretask_alias(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    pretask = paths.repo_root / PRETASK_DIRNAME
    pretask.mkdir()
    (pretask / "bundle-note.txt").write_text("Explore checkout and account pages\n", encoding="utf-8")

    files = list_inbox_files(paths)

    assert [str(f.relative_to(paths.repo_root)) for f in files] == [
        f"{PRETASK_DIRNAME}/bundle-note.txt"
    ]


def test_dashboard_inbox_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Upload + ingest through the HTTP surface end-to-end."""
    paths = _runtime(tmp_path, enable_write=True)
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        _wait(base + "/healthz", timeout=5).read()

        # Initially empty.
        with urllib.request.urlopen(base + "/api/inbox", timeout=4) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["files"] == []
        assert ".md" in payload["supported_extensions"]

        # Upload a markdown doc.
        body = "# Upload-route smoke\n\nPriority: P1\nSUT root: .\n\n## Expected behavior\nAPI responds.\n"
        upload_payload = {
            "filename": "smoke.md",
            "content_base64": base64.b64encode(body.encode("utf-8")).decode("ascii"),
        }
        req = urllib.request.Request(
            base + "/api/inbox/upload",
            data=json.dumps(upload_payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            up = json.loads(resp.read().decode("utf-8"))
        assert up["ok"] is True
        assert up["path"].endswith("smoke.md")

        # Pending list now sees the file.
        with urllib.request.urlopen(base + "/api/inbox", timeout=4) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert len(payload["files"]) == 1
        assert payload["files"][0]["name"] == "smoke.md"

        # Ingest.
        ingest_req = urllib.request.Request(
            base + "/api/inbox/ingest",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(ingest_req, timeout=5) as resp:
            ingest_payload = json.loads(resp.read().decode("utf-8"))
        assert ingest_payload["created"] == 1
        assert ingest_payload["failed"] == 0
        assert ingest_payload["results"][0]["title"] == "Upload-route smoke"
    finally:
        srv._shutdown_requested = True
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def test_synthesize_inbox_creates_one_task_from_multiple_docs(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    inbox = paths.repo_root / INBOX_DIRNAME
    pretask = paths.repo_root / PRETASK_DIRNAME
    pretask.mkdir()
    feature = inbox / "feature.md"
    feature.write_text(
        "# Quality blog feature brief\n\n"
        "Priority: P1\n"
        "SUT root: .\n\n"
        "## Expected behavior\n"
        "- Homepage must expose newest posts and navigation.\n"
        "- GET /rss.xml should return the feed.\n",
        encoding="utf-8",
    )
    note = pretask / "qa-notes.txt"
    note.write_text(
        "Exploratory checks\n\n"
        "Validate /blog, /about and /contact pages.\n"
        "Known bug: broken image on one article should be reported.\n",
        encoding="utf-8",
    )

    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        result = synthesize_inbox_task(
            conn,
            paths,
            events,
            title="Quality blog synthesized QA task",
        )
        assert result["status"] == "created"
        assert result["created"] == 1
        assert result["failed"] == 0
        assert result["source_count"] == 2
        row = conn.execute(
            "SELECT title, priority, spec_path FROM work_items WHERE id=?;",
            (result["work_item_id"],),
        ).fetchone()
        assert row["title"] == "Quality blog synthesized QA task"
        assert row["priority"] == "P1"
        spec = (paths.repo_root / row["spec_path"]).read_text(encoding="utf-8")
        assert "## Source documents" in spec
        assert "`inbox/feature.md`" in spec
        assert "`pretask/qa-notes.txt`" in spec
        assert "GET /rss.xml" in spec
        assert "/blog" in spec
        assert "Known bug: broken image" in spec
    finally:
        conn.close()
    assert not feature.exists()
    assert not note.exists()
    assert list((inbox / ARCHIVE_DIRNAME).glob("feature-*.md"))
    assert list((pretask / ARCHIVE_DIRNAME).glob("qa-notes-*.txt"))


def test_dashboard_inbox_synthesize_endpoint(tmp_path: Path) -> None:
    paths = _runtime(tmp_path, enable_write=True)
    (paths.repo_root / INBOX_DIRNAME / "docs.md").write_text(
        "# Dashboard synthesis\n\nPriority: P2\n\n"
        "## Expected behavior\nUser should be able to create a task from uploaded docs.\n",
        encoding="utf-8",
    )
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        _wait(base + "/healthz", timeout=5).read()
        req = urllib.request.Request(
            base + "/api/inbox/synthesize",
            data=json.dumps({"title": "Dashboard synthesized task"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["status"] == "created"
        assert payload["created"] == 1
        assert payload["title"] == "Dashboard synthesized task"
        assert payload["results"][0]["work_item_id"] == payload["work_item_id"]
    finally:
        srv._shutdown_requested = True
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def test_cli_inbox_synthesize_returns_created_json(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    (paths.repo_root / PRETASK_DIRNAME).mkdir()
    (paths.repo_root / PRETASK_DIRNAME / "cli-note.txt").write_text(
        "CLI synthesize\n\nUser should see one generated task for a documentation bundle.\n",
        encoding="utf-8",
    )

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main([
            "--root",
            str(paths.repo_root),
            "--json",
            "inbox",
            "synthesize",
            "--title",
            "CLI synthesized task",
        ])

    assert rc == 0, err.getvalue()
    payload = json.loads(out.getvalue())
    assert payload["status"] == "created"
    assert payload["title"] == "CLI synthesized task"
    assert payload["work_item_id"].startswith("TASK-")


def test_supported_extensions_constant_is_stable() -> None:
    assert SUPPORTED_EXTS == {".md", ".markdown", ".txt", ".docx", ".pdf"}


def test_ingest_honors_markdown_priority_and_sut_root(tmp_path: Path) -> None:
    """Markdown spec with `Priority: P0` and `SUT root: <other>` must persist
    those values rather than the inbox defaults (Codex review #48 P2)."""
    paths = _runtime(tmp_path)
    inbox = paths.repo_root / INBOX_DIRNAME
    sut_dir = paths.repo_root / "services" / "api"
    sut_dir.mkdir(parents=True, exist_ok=True)
    src = inbox / "p0-spec.md"
    src.write_text(
        "# Critical payments outage\n\n"
        "Priority: P0\n"
        "SUT root: services/api\n\n"
        "## Expected behavior\nPayments do not 500 under burst load.\n",
        encoding="utf-8",
    )
    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        results = ingest_inbox(conn, paths, events)
        assert len(results) == 1
        r = results[0]
        assert r["status"] == "created"
        row = conn.execute(
            "SELECT priority, sut_root FROM work_items WHERE id=?;",
            (r["work_item_id"],),
        ).fetchone()
        assert row["priority"] == "P0"
        assert row["sut_root"] == "services/api"
    finally:
        conn.close()


def test_ingest_continues_after_parser_library_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single malformed PDF must not abort the batch (Codex review #48 P1).
    Subsequent files in the inbox must still be processed."""
    from agentic_os import inbox as inbox_module

    paths = _runtime(tmp_path)
    inbox = paths.repo_root / INBOX_DIRNAME
    (inbox / "broken.pdf").write_bytes(b"%PDF-not-actually-a-pdf")
    (inbox / "ok.md").write_text(
        "# Survivor\n\nPriority: P2\nSUT root: .\n\n## Expected behavior\nOK.\n",
        encoding="utf-8",
    )

    def _boom(_path: Path) -> str:
        raise RuntimeError("pypdf exploded on malformed header")

    monkeypatch.setattr(inbox_module, "_extract_pdf", _boom)

    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        results = ingest_inbox(conn, paths, events)
    finally:
        conn.close()
    assert len(results) == 2
    by_name = {r["source"].split("/")[-1]: r for r in results}
    assert by_name["broken.pdf"]["status"] == "failed"
    assert "pdf parser failed" in by_name["broken.pdf"]["error"]
    assert "RuntimeError" in by_name["broken.pdf"]["error"]
    assert by_name["ok.md"]["status"] == "created"
    assert by_name["ok.md"]["title"] == "Survivor"


class _FakePdfPage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakePdfReader:
    def __init__(self, pages_text):
        self.pages = [_FakePdfPage(t) for t in pages_text]


def _install_fake_pypdf(monkeypatch: pytest.MonkeyPatch, pages_text):
    import sys
    import types

    module = types.ModuleType("pypdf")

    def _reader(_path):
        return _FakePdfReader(pages_text)

    module.PdfReader = _reader  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pypdf", module)


def test_inspect_pdf_classifies_scanned_pdf_as_low(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PDF whose pages yield no text density is reported as `low` so the
    ingest path quarantines it with the 'OCR not supported' hint instead of
    silently producing an empty task."""
    from agentic_os.inbox import inspect_pdf

    paths = _runtime(tmp_path)
    src = paths.repo_root / INBOX_DIRNAME / "scan.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    _install_fake_pypdf(monkeypatch, ["", "", ""])

    result = inspect_pdf(src)
    assert result.status == EXTRACTION_STATUS_LOW
    assert result.pages == 3
    assert result.chars == 0
    assert "Scanned PDFs are not supported" in (result.message or "")


def test_inspect_pdf_classifies_text_pdf_as_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_os.inbox import inspect_pdf

    paths = _runtime(tmp_path)
    src = paths.repo_root / INBOX_DIRNAME / "doc.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    pages = ["This is a paragraph with enough text to exceed the density threshold." * 2]
    _install_fake_pypdf(monkeypatch, pages)

    result = inspect_pdf(src)
    assert result.status == EXTRACTION_STATUS_OK
    assert result.chars > 0
    assert result.density >= 50


def test_ingest_quarantines_scanned_pdf_with_ocr_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #143: scanned PDFs must NOT silently create empty tasks. They
    must move to `.failed/` with a sidecar that names the OCR limit so the
    operator understands why the document was rejected."""
    paths = _runtime(tmp_path)
    src = paths.repo_root / INBOX_DIRNAME / "scan.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    _install_fake_pypdf(monkeypatch, ["", ""])

    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        results = ingest_inbox(conn, paths, events)
    finally:
        conn.close()
    assert len(results) == 1
    r = results[0]
    assert r["status"] == "failed"
    assert "Scanned PDFs are not supported" in r["error"]
    assert r["archived_to"].startswith(f"{INBOX_DIRNAME}/{FAILED_DIRNAME}/")
    sidecars = list((paths.repo_root / INBOX_DIRNAME / FAILED_DIRNAME).glob("*.error.txt"))
    assert len(sidecars) == 1
    assert "Scanned PDFs are not supported" in sidecars[0].read_text(encoding="utf-8")


def test_classify_intake_file_reports_per_file_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _runtime(tmp_path)
    md = paths.repo_root / INBOX_DIRNAME / "spec.md"
    md.write_text("# Title\n", encoding="utf-8")
    scan = paths.repo_root / INBOX_DIRNAME / "scan.pdf"
    scan.write_bytes(b"%PDF-1.4 fake")
    _install_fake_pypdf(monkeypatch, ["", ""])

    md_info = classify_intake_file(md)
    pdf_info = classify_intake_file(scan)
    assert md_info["status"] == EXTRACTION_STATUS_OK
    assert pdf_info["status"] == EXTRACTION_STATUS_LOW
    assert "Scanned PDFs are not supported" in pdf_info["message"]


def test_classify_intake_reports_failed_when_pypdf_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators without `pypdf` installed must see a clear status badge
    instead of `extract: ?` so they know the limit is installable."""
    import builtins

    paths = _runtime(tmp_path)
    src = paths.repo_root / INBOX_DIRNAME / "doc.pdf"
    src.write_bytes(b"%PDF-1.4 fake")

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("simulated missing pypdf")
        return real_import(name, *args, **kwargs)

    import sys
    monkeypatch.delitem(sys.modules, "pypdf", raising=False)
    monkeypatch.setattr(builtins, "__import__", _blocked)

    info = classify_intake_file(src)
    assert info["status"] == EXTRACTION_STATUS_FAILED
    assert "pypdf" in info["message"]


def test_inbox_list_endpoint_returns_extraction_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`/api/inbox` must carry per-file extraction status so the dashboard
    can warn the operator about scanned PDFs without uploading them first."""
    paths = _runtime(tmp_path, enable_write=True)
    (paths.repo_root / INBOX_DIRNAME / "scan.pdf").write_bytes(b"%PDF-1.4 fake")
    (paths.repo_root / INBOX_DIRNAME / "ok.md").write_text("# Hi\n", encoding="utf-8")
    _install_fake_pypdf(monkeypatch, ["", ""])

    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        _wait(base + "/healthz", timeout=5).read()
        with urllib.request.urlopen(base + "/api/inbox", timeout=4) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        by_name = {f["name"]: f for f in payload["files"]}
        assert by_name["ok.md"]["extraction"]["status"] == EXTRACTION_STATUS_OK
        assert by_name["scan.pdf"]["extraction"]["status"] == EXTRACTION_STATUS_LOW
        assert "Scanned PDFs are not supported" in by_name["scan.pdf"]["extraction"]["message"]
    finally:
        srv._shutdown_requested = True
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Issue #157 — auto-fire crawler for `Type: public-site` intake docs.
# ---------------------------------------------------------------------------


def _public_site_server():
    """Tiny HTTP server emitting a small route graph with one 404 image."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    pages = {
        "/": (
            200,
            "text/html",
            '<!doctype html><html><body>'
            '<a href="/about">About</a>'
            '<a href="/blog">Blog</a>'
            '<img src="/missing.png">'
            '</body></html>',
        ),
        "/about": (200, "text/html", "<!doctype html><html><body>about</body></html>"),
        "/blog": (200, "text/html", "<!doctype html><html><body>blog</body></html>"),
    }

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            page = pages.get(self.path)
            if page is None:
                self.send_error(404, "missing")
                return
            status, ctype, body = page
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_HEAD(self):  # noqa: N802
            page = pages.get(self.path)
            if page is None:
                self.send_error(404, "missing")
                return
            status, ctype, _ = page
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.end_headers()

        def log_message(self, *_a, **_kw):  # pragma: no cover
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    return srv, thread, base


def test_synthesize_inbox_fires_crawler_for_public_site_intake(tmp_path: Path) -> None:
    """A markdown intake doc tagged `Type: public-site` triggers a same-origin
    HTTP crawl during synthesis. Discovered routes land in the spec's
    "Relevant endpoints or pages" section and the JSON report is persisted
    under `agentic-os-runtime/inbox/crawls/<work_item_id>/`."""
    srv, thread, base = _public_site_server()
    try:
        paths = _runtime(tmp_path)
        brief = paths.repo_root / INBOX_DIRNAME / "site-brief.md"
        brief.write_text(
            "# Public site QA sweep\n\n"
            "Priority: P2\n"
            "SUT root: .\n"
            "Type: public-site\n"
            f"Start URL: {base}/\n\n"
            "## Expected behavior\n"
            "Smoke crawl the public site and report broken assets.\n",
            encoding="utf-8",
        )

        conn = connect(paths.db)
        try:
            events = EventLog(conn, paths)
            result = synthesize_inbox_task(
                conn,
                paths,
                events,
                allow_private_crawl=True,  # tmp HTTP server on loopback
            )
        finally:
            conn.close()

        assert result["status"] == "created"
        crawled = result.get("crawled_sites") or []
        assert len(crawled) == 1
        assert crawled[0]["status"] == "ok"
        assert crawled[0]["start_url"].startswith(base)
        assert crawled[0]["pages_visited"] >= 1
        # The broken `/missing.png` asset should be surfaced.
        assert crawled[0]["broken_assets_total"] >= 1

        report_paths = result.get("crawl_reports") or []
        assert len(report_paths) == 1
        report_abs = paths.repo_root / report_paths[0]
        assert report_abs.exists()
        report_json = json.loads(report_abs.read_text(encoding="utf-8"))
        urls = {r["url"] for r in report_json["routes"]}
        assert any(u.endswith("/about") for u in urls)
        assert any(u.endswith("/blog") for u in urls)

        # Spec markdown carries crawler-discovered routes + broken asset note.
        conn = connect(paths.db)
        try:
            row = conn.execute(
                "SELECT spec_path FROM work_items WHERE id=?;",
                (result["work_item_id"],),
            ).fetchone()
        finally:
            conn.close()
        spec = (paths.repo_root / row["spec_path"]).read_text(encoding="utf-8")
        assert "/about" in spec
        assert "/blog" in spec
        assert "broken" in spec.lower()
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def test_synthesize_inbox_records_skip_when_public_site_lacks_start_url(
    tmp_path: Path,
) -> None:
    """A `Type: public-site` doc without `Start URL:` still creates a task
    (the markdown body is the brief) but records a skipped crawl entry so
    the operator notices the missing metadata."""
    paths = _runtime(tmp_path)
    brief = paths.repo_root / INBOX_DIRNAME / "site-brief.md"
    brief.write_text(
        "# Public site placeholder\n\n"
        "Priority: P3\n"
        "Type: public-site\n\n"  # no Start URL
        "## Expected behavior\n"
        "Placeholder — Start URL pending stakeholder confirmation.\n",
        encoding="utf-8",
    )

    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        result = synthesize_inbox_task(conn, paths, events)
    finally:
        conn.close()

    assert result["status"] == "created"
    crawled = result.get("crawled_sites") or []
    assert len(crawled) == 1
    assert crawled[0]["status"] == "skipped"
    assert "Start URL" in (crawled[0].get("error") or "")


def test_synthesize_inbox_records_failure_on_ssrf_refusal(tmp_path: Path) -> None:
    """Default ``allow_private_crawl=False`` refuses loopback start URLs.
    The intake doc still ingests but the crawl is recorded as ``failed``
    so the operator sees the safety guard fired."""
    paths = _runtime(tmp_path)
    brief = paths.repo_root / INBOX_DIRNAME / "loopback-brief.md"
    brief.write_text(
        "# Loopback target\n\n"
        "Priority: P2\n"
        "Type: public-site\n"
        "Start URL: http://127.0.0.1:9/\n\n"
        "## Expected behavior\n"
        "Should never reach loopback by default.\n",
        encoding="utf-8",
    )

    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        result = synthesize_inbox_task(conn, paths, events)  # allow_private_crawl=False
    finally:
        conn.close()

    assert result["status"] == "created"
    crawled = result.get("crawled_sites") or []
    assert len(crawled) == 1
    assert crawled[0]["status"] == "failed"
    assert "loopback" in (crawled[0].get("error") or "").lower() or \
           "private" in (crawled[0].get("error") or "").lower()
