"""OpenAPI parser.

Reads a local OpenAPI spec (YAML or JSON), normalizes it into a compact
inventory dict consumed by analyze/plan. Network URL fetch is intentionally
out of scope for MVP — operator configures `sut.openapi.sources` with a
`type: file` entry; the URL path will be re-enabled in a follow-up once
fetch is properly guarded.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import ConfigError, UsageError

try:
    import yaml  # type: ignore[import-untyped]
except ImportError as exc:  # pragma: no cover
    raise ConfigError("PyYAML required for OpenAPI parsing") from exc


_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


@dataclass(frozen=True)
class OpenAPIOperation:
    path: str
    method: str
    operation_id: Optional[str]
    summary: Optional[str]
    tags: List[str]
    parameters: List[Dict[str, Any]] = field(default_factory=list)
    request_body: Optional[Dict[str, Any]] = None
    responses: Dict[str, Any] = field(default_factory=dict)
    security: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class OpenAPIInventory:
    source_path: str
    source_hash: str
    title: Optional[str]
    version: Optional[str]
    operations: List[OpenAPIOperation]
    security_schemes: Dict[str, Any]
    raw_size_bytes: int


def load_openapi_file(path: Path) -> OpenAPIInventory:
    """Load OpenAPI spec from a local file. Supports .yaml/.yml/.json."""
    if not path.exists() or not path.is_file():
        raise UsageError(f"openapi file not found: {path}")
    suffix = path.suffix.lower()
    raw_bytes = path.read_bytes()
    if suffix in {".yaml", ".yml"}:
        try:
            data = yaml.safe_load(raw_bytes.decode("utf-8"))
        except yaml.YAMLError as exc:
            raise UsageError(f"invalid YAML in {path}: {exc}") from exc
    elif suffix == ".json":
        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise UsageError(f"invalid JSON in {path}: {exc}") from exc
    else:
        raise UsageError(f"unsupported openapi extension: {suffix}")
    if not isinstance(data, dict):
        raise UsageError(f"openapi root must be a mapping in {path}")
    return _normalize(data, source_path=str(path), source_bytes=raw_bytes)


def _normalize(
    data: Dict[str, Any],
    *,
    source_path: str,
    source_bytes: bytes,
) -> OpenAPIInventory:
    info = data.get("info") or {}
    paths = data.get("paths") or {}
    components = data.get("components") or {}
    security_schemes = components.get("securitySchemes") or {}

    operations: List[OpenAPIOperation] = []
    if isinstance(paths, dict):
        for path_str, ops in paths.items():
            if not isinstance(ops, dict):
                continue
            path_level_params = ops.get("parameters") or []
            for method, body in ops.items():
                method_lower = method.lower()
                if method_lower not in _HTTP_METHODS:
                    continue
                if not isinstance(body, dict):
                    continue
                op_params = list(path_level_params) + list(body.get("parameters") or [])
                operations.append(
                    OpenAPIOperation(
                        path=str(path_str),
                        method=method_lower,
                        operation_id=body.get("operationId"),
                        summary=body.get("summary"),
                        tags=list(body.get("tags") or []),
                        parameters=op_params,
                        request_body=body.get("requestBody"),
                        responses=dict(body.get("responses") or {}),
                        security=list(body.get("security") or []),
                    )
                )

    digest = hashlib.sha256(source_bytes).hexdigest()
    return OpenAPIInventory(
        source_path=source_path,
        source_hash=digest,
        title=info.get("title"),
        version=info.get("version"),
        operations=operations,
        security_schemes=dict(security_schemes) if isinstance(security_schemes, dict) else {},
        raw_size_bytes=len(source_bytes),
    )


def inventory_to_dict(inv: OpenAPIInventory) -> Dict[str, Any]:
    """Serialize inventory to JSON-able dict for sut-map.json."""
    return {
        "source_path": inv.source_path,
        "source_hash": inv.source_hash,
        "title": inv.title,
        "version": inv.version,
        "raw_size_bytes": inv.raw_size_bytes,
        "operations": [
            {
                "path": op.path,
                "method": op.method,
                "operation_id": op.operation_id,
                "summary": op.summary,
                "tags": list(op.tags),
                "parameters": op.parameters,
                "request_body": op.request_body,
                "responses": op.responses,
                "security": op.security,
            }
            for op in inv.operations
        ],
        "security_schemes": inv.security_schemes,
    }
