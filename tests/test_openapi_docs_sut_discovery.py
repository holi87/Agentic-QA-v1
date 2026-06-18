"""OpenAPI loading, local documentation ingest, and SUT discovery behavior."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from agentic_os.docs_ingest import MAX_DOC_BYTES, ingest_local_doc, ingested_to_dict
from agentic_os.errors import UsageError
from agentic_os.openapi import inventory_to_dict, load_openapi_file
from agentic_os.sut_discovery import (
    discover_sut,
    discovery_to_dict,
    recommended_runners,
)


_OPENAPI_YAML = textwrap.dedent(
    """\
    openapi: 3.0.0
    info:
      title: Orders API
      version: "1.0"
    paths:
      /orders:
        post:
          operationId: createOrder
          summary: Create order
          tags: [orders]
          requestBody:
            required: true
            content:
              application/json:
                schema: {type: object}
          responses:
            "201": {description: created}
            "400": {description: invalid input}
          security:
            - bearerAuth: []
        get:
          operationId: listOrders
          responses:
            "200": {description: ok}
    components:
      securitySchemes:
        bearerAuth:
          type: http
          scheme: bearer
    """
)


def test_openapi_parses_yaml_file(tmp_path: Path) -> None:
    src = tmp_path / "openapi.yaml"
    src.write_text(_OPENAPI_YAML, encoding="utf-8")
    inv = load_openapi_file(src)
    assert inv.title == "Orders API"
    assert inv.version == "1.0"
    assert len(inv.operations) == 2
    paths = {(o.path, o.method) for o in inv.operations}
    assert ("/orders", "post") in paths
    assert ("/orders", "get") in paths
    assert "bearerAuth" in inv.security_schemes
    assert inv.source_hash  # sha256 hex


def test_openapi_parses_json_file(tmp_path: Path) -> None:
    src = tmp_path / "openapi.json"
    body = {
        "openapi": "3.0.0",
        "info": {"title": "J", "version": "1"},
        "paths": {"/x": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    src.write_text(json.dumps(body), encoding="utf-8")
    inv = load_openapi_file(src)
    assert inv.title == "J"
    assert inv.operations[0].path == "/x"


def test_openapi_rejects_unknown_extension(tmp_path: Path) -> None:
    src = tmp_path / "spec.xml"
    src.write_text("<x/>", encoding="utf-8")
    with pytest.raises(UsageError):
        load_openapi_file(src)


def test_openapi_inventory_to_dict_is_jsonable(tmp_path: Path) -> None:
    src = tmp_path / "openapi.yaml"
    src.write_text(_OPENAPI_YAML, encoding="utf-8")
    inv = load_openapi_file(src)
    d = inventory_to_dict(inv)
    json.dumps(d)  # must not raise


def test_docs_ingest_md_extracts_sections(tmp_path: Path) -> None:
    doc = tmp_path / "requirements.md"
    doc.write_text(
        textwrap.dedent(
            """\
            # Orders feature

            ## Validation

            Quantity must be positive.

            ## Auth

            Bearer token required.
            """
        ),
        encoding="utf-8",
    )
    ingested = ingest_local_doc(doc)
    headings = [s.heading for s in ingested.sections]
    assert "Orders feature" in headings
    assert "Validation" in headings
    assert "Auth" in headings
    assert ingested.source_hash
    assert ingested.media_type == "text/markdown"


def test_docs_ingest_rejects_unsupported_extension(tmp_path: Path) -> None:
    doc = tmp_path / "weird.docx"
    doc.write_text("not really docx", encoding="utf-8")
    with pytest.raises(UsageError):
        ingest_local_doc(doc)


def test_docs_ingest_blocks_oversized_doc(tmp_path: Path) -> None:
    doc = tmp_path / "big.md"
    doc.write_bytes(b"x" * (MAX_DOC_BYTES + 1))
    with pytest.raises(UsageError) as exc:
        ingest_local_doc(doc)
    assert "ceiling" in str(exc.value)


def test_docs_ingest_to_dict_redacts_body() -> None:
    # ingested_to_dict must never put raw body in the serialized form.
    from agentic_os.docs_ingest import DocSection, IngestedDoc

    doc = IngestedDoc(
        source_path="x.md",
        source_hash="h",
        ingested_at="2026-05-19T00:00:00Z",
        size_bytes=4,
        media_type="text/markdown",
        sections=[DocSection(heading="A", body="raw secret body")],
        text="full text",
    )
    d = ingested_to_dict(doc)
    assert "raw secret body" not in json.dumps(d)
    assert "full text" not in json.dumps(d)


def test_sut_discovery_classifies_node(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "orders.spec.ts").write_text("// spec\n", encoding="utf-8")
    d = discover_sut(tmp_path)
    assert d.stack == "node"
    runners = recommended_runners(d.stack)
    assert runners == ("playwright-ts", "playwright-ts")
    assert any(t.runner == "playwright" for t in d.tests)


def test_sut_discovery_classifies_python(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_orders.py").write_text("def test_x(): pass\n", encoding="utf-8")
    d = discover_sut(tmp_path)
    assert d.stack == "python"
    assert recommended_runners(d.stack) == ("pytest-httpx", "playwright-ts")
    assert any(t.runner == "pytest" for t in d.tests)


def test_sut_discovery_classifies_mixed(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    d = discover_sut(tmp_path)
    assert d.stack == "mixed"


def test_sut_discovery_skips_node_modules(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    nm = tmp_path / "node_modules" / "react"
    nm.mkdir(parents=True)
    (nm / "package.json").write_text('{"name":"react"}', encoding="utf-8")
    d = discover_sut(tmp_path)
    # Only the root package.json is in markers — node_modules is skipped.
    assert d.markers["node"] == ["package.json"]


def test_sut_discovery_to_dict_includes_recommendation(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    d = discover_sut(tmp_path)
    payload = discovery_to_dict(d)
    assert payload["recommended"]["api_runner"] == "pytest-httpx"
    assert payload["stack"] == "python"
