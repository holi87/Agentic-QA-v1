"""System-under-test map construction.

Split from analysis.py (issue #292).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ..autonomy.concurrency import ConcurrencyController, fan_out
from ..paths import RuntimePaths
from ..time_utils import now_iso

from .extractors import _API_HINT
from .inputs import _safe_fetch_url
from .types import _AnalysisInputs

# Issue #359 — the three SUT-map probes are independent (each produces a
# disjoint slice of the map and touches no shared state, no DB, no events), so
# they fan out under the planner role's concurrency budget. The join barrier in
# this function composes the slices in a FIXED order regardless of completion
# order — keeping the map deterministic and byte-equivalent to the serial path.
_PROBE_ROLE = "planner"


def _build_sut_map(
    paths: RuntimePaths,
    inputs: _AnalysisInputs,
    *,
    controller: Optional[ConcurrencyController] = None,
) -> Dict[str, Any]:
    sources: List[Dict[str, Any]] = []
    cfg = inputs.config_snapshot

    def _record(label: str, rel: Any) -> None:
        if not rel or not isinstance(rel, str):
            return
        target = (paths.repo_root / rel).resolve()
        try:
            target.relative_to(paths.repo_root.resolve())
        except ValueError:
            sources.append({"label": label, "path": rel, "status": "outside_repo"})
            return
        status = "missing"
        if target.exists():
            status = "file" if target.is_file() else "dir"
        sources.append({"label": label, "path": rel, "status": status})

    # v2 openapi/docs are dicts with `sources`; record each source file separately.
    openapi_cfg = cfg.get("openapi")
    if isinstance(openapi_cfg, str):
        _record("openapi", openapi_cfg)
    elif isinstance(openapi_cfg, dict):
        for src in openapi_cfg.get("sources") or []:
            if isinstance(src, dict) and src.get("type") == "file":
                _record("openapi_source", src.get("value"))
    docs_cfg = cfg.get("docs")
    if isinstance(docs_cfg, str):
        _record("docs", docs_cfg)
    elif isinstance(docs_cfg, dict):
        for src in docs_cfg.get("sources") or []:
            if isinstance(src, dict) and src.get("type") == "file":
                _record("docs_source", src.get("value"))
    _record("tests_dir", cfg.get("tests_dir"))
    _record("sut_root", cfg.get("sut_root"))
    endpoints = sorted({m.group(0) for m in _API_HINT.finditer(inputs.spec_markdown)})

    sut_block = inputs.config_snapshot
    openapi_block = (sut_block.get("openapi") or {}) if isinstance(sut_block.get("openapi"), dict) else {}
    docs_block = (sut_block.get("docs") or {}) if isinstance(sut_block.get("docs"), dict) else {}

    # --- probe thunks (pure: no DB, no events, no shared mutable state) ------
    # Each returns its own slice of the map. Bodies are byte-for-byte the prior
    # serial logic, including the inner per-source `except Exception` masking,
    # so a single bad source still degrades gracefully inside its probe. An
    # *unexpected* probe-level failure escapes the thunk and is captured by
    # fan_out as a gap (see the join barrier below) — it never unwinds the map.

    def _openapi_probe() -> List[Dict[str, Any]]:
        try:
            from ..openapi import inventory_to_dict, load_openapi_file
        except ImportError:
            return []
        inventory: List[Dict[str, Any]] = []
        for src in (openapi_block.get("sources") or []):
            if not isinstance(src, dict):
                continue
            src_type = src.get("type")
            value = src.get("value")
            if not value:
                continue
            if src_type == "file":
                target = (paths.repo_root / value).resolve()
                try:
                    target.relative_to(paths.repo_root.resolve())
                except ValueError:
                    continue
                if not target.is_file():
                    continue
                try:
                    inv = load_openapi_file(target)
                    inventory.append(inventory_to_dict(inv))
                except Exception as exc:
                    inventory.append({"source_path": str(value), "error": str(exc)})
            elif src_type == "url":
                # Issue #78 — guarded URL fetch with timeout, size cap,
                # content-type sanity check, and a refusal for
                # localhost/private-network targets unless the operator
                # explicitly allowed them via `allow_private`.
                try:
                    fetched_text = _safe_fetch_url(
                        value,
                        allow_private=bool(src.get("allow_private")),
                    )
                    target_tmp = paths.runtime_root / "openapi-cache"
                    target_tmp.mkdir(parents=True, exist_ok=True)
                    import hashlib as _hashlib

                    digest = _hashlib.sha256(fetched_text.encode("utf-8")).hexdigest()
                    cache_file = target_tmp / f"{digest[:16]}.yaml"
                    cache_file.write_text(fetched_text, encoding="utf-8")
                    inv = load_openapi_file(cache_file)
                    data = inventory_to_dict(inv)
                    data["source_path"] = value
                    data["source_sha256"] = digest
                    inventory.append(data)
                except Exception as exc:
                    inventory.append({"source_path": str(value), "error": str(exc)})
        return inventory

    def _docs_probe() -> List[Dict[str, Any]]:
        try:
            from ..docs_ingest import ingest_local_doc, ingested_to_dict
        except ImportError:
            return []
        inventory: List[Dict[str, Any]] = []
        for src in (docs_block.get("sources") or []):
            if not isinstance(src, dict):
                continue
            src_type = src.get("type")
            value = src.get("value")
            if not value:
                continue
            if src_type == "url":
                # Issue #78 — guarded URL fetch for docs sources.
                try:
                    fetched = _safe_fetch_url(
                        value,
                        allow_private=bool(src.get("allow_private")),
                    )
                    cache_dir = paths.runtime_root / "docs-cache"
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    import hashlib as _hashlib

                    digest = _hashlib.sha256(fetched.encode("utf-8")).hexdigest()
                    cache_file = cache_dir / f"{digest[:16]}.md"
                    cache_file.write_text(fetched, encoding="utf-8")
                    doc = ingest_local_doc(cache_file)
                    data = ingested_to_dict(doc)
                    data["source_path"] = value
                    data["source_sha256"] = digest
                    inventory.append(data)
                except Exception as exc:
                    inventory.append({"source_path": str(value), "error": str(exc)})
                continue
            if src_type != "file":
                continue
            target = (paths.repo_root / value).resolve()
            try:
                target.relative_to(paths.repo_root.resolve())
            except ValueError:
                continue
            if not target.is_file():
                continue
            try:
                doc = ingest_local_doc(target)
                inventory.append(ingested_to_dict(doc))
            except Exception as exc:
                inventory.append({"source_path": str(value), "error": str(exc)})
        return inventory

    def _discovery_probe() -> Optional[Dict[str, Any]]:
        try:
            from ..sut_discovery import discover_sut, discovery_to_dict
        except ImportError:
            return None
        sut_root_str = cfg.get("sut_root") or "."
        sut_root_path = (paths.repo_root / sut_root_str).resolve()
        try:
            sut_root_path.relative_to(paths.repo_root.resolve())
        except ValueError:
            # sut_root escapes the repo — a legitimate skip, not a probe gap.
            return None
        return discovery_to_dict(discover_sut(sut_root_path))

    # --- fan-out + join barrier ---------------------------------------------
    if controller is None:
        controller = _default_controller(paths)

    probe_names = ("openapi", "docs", "discovery")
    probes: List[Callable[[], Any]] = [_openapi_probe, _docs_probe, _discovery_probe]
    results = fan_out(controller, _PROBE_ROLE, probes)

    # Compose in fixed (name) order, independent of completion order. A failed
    # probe leaves its slice empty and records a gap; the caller (which owns
    # `events`) emits the gap event — threads stay event/DB-free.
    slices: List[Any] = [None, None, None]
    probe_gaps: List[Dict[str, str]] = []
    for result in results:
        if result.ok:
            slices[result.index] = result.value
        else:
            probe_gaps.append(
                {"probe": probe_names[result.index], "error": str(result.error)}
            )
    openapi_inventory = slices[0] or []
    docs_inventory = slices[1] or []
    discovery_payload = slices[2]  # None is a valid (non-gap) outcome

    return {
        "generated_at": now_iso(),
        "work_item_id": inputs.work_item["id"],
        "spec_path": inputs.work_item["spec_path"],
        "sut_root": cfg.get("sut_root"),
        "config_snapshot": cfg,
        "config_warning": inputs.config_warning,
        "sources": sources,
        "endpoints_from_spec": endpoints,
        "openapi_inventory": openapi_inventory,
        "docs_inventory": docs_inventory,
        "discovery": discovery_payload,
        "probe_gaps": probe_gaps,
    }


def _default_controller(paths: RuntimePaths) -> ConcurrencyController:
    """Build the live planner controller from config (issue #359).

    The probes do not call models, so no backpressure check is wired. If config
    cannot be loaded, fall back to a permissive controller so analysis never
    fails to *start* over a concurrency-config problem.
    """
    try:
        from ..autonomy.concurrency import build_concurrency_controller
        from ..config import load_or_default

        return build_concurrency_controller(load_or_default(paths.repo_root))
    except Exception:
        return ConcurrencyController(global_limit=4)
