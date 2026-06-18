"""SUT stack discovery.

Classifies the SUT into one of `node | python | mixed | unknown` and scans
for existing test files. Outputs are deterministic — sorted by relative path
so sut-map.json diffs cleanly between runs.

ADR 0003 owns the mapping from stack to default API/UI runner; this module
only reports facts.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


_NODE_MARKERS = ("package.json",)
_PY_MARKERS = ("pyproject.toml", "setup.cfg", "setup.py")
_PY_REQUIREMENTS = ("requirements.txt", "requirements-dev.txt")

_PYTEST_TEST_RE = ("test_", "_test.py")
_PLAYWRIGHT_PATTERNS = (".spec.ts", ".spec.js", ".test.ts", ".test.js")
_CUCUMBER_PATTERNS = (".feature",)

# Bound the walk so a misconfigured `sut.root` does not enumerate node_modules.
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".tox",
    ".pytest_cache",
    "build",
    "dist",
    "target",
    ".gradle",
}


@dataclass(frozen=True)
class TestFileHit:
    path: str
    runner: str  # pytest | playwright | cucumber | unknown


@dataclass(frozen=True)
class SutDiscovery:
    sut_root: str
    stack: str  # node | python | mixed | unknown
    markers: Dict[str, List[str]]
    tests: List[TestFileHit]
    notes: List[str] = field(default_factory=list)


def discover_sut(sut_root: Path, *, max_files: int = 5000) -> SutDiscovery:
    """Walk sut_root and produce a deterministic discovery snapshot."""
    if not sut_root.exists():
        return SutDiscovery(
            sut_root=str(sut_root),
            stack="unknown",
            markers={},
            tests=[],
            notes=[f"sut_root does not exist: {sut_root}"],
        )
    markers: Dict[str, List[str]] = {"node": [], "python": []}
    tests: List[TestFileHit] = []
    notes: List[str] = []
    visited = 0

    for dirpath, dirnames, filenames in os.walk(sut_root):
        # Filter skip dirs in-place so os.walk does not descend.
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            visited += 1
            if visited > max_files:
                notes.append(f"discovery truncated at {max_files} files")
                return SutDiscovery(
                    sut_root=str(sut_root),
                    stack=_classify(markers),
                    markers=markers,
                    tests=sorted(tests, key=lambda t: t.path),
                    notes=notes,
                )
            rel = os.path.relpath(os.path.join(dirpath, name), sut_root)
            if name in _NODE_MARKERS:
                markers["node"].append(rel)
            if name in _PY_MARKERS or name in _PY_REQUIREMENTS:
                markers["python"].append(rel)
            hit = _classify_test_file(name)
            if hit is not None:
                tests.append(TestFileHit(path=rel, runner=hit))
    return SutDiscovery(
        sut_root=str(sut_root),
        stack=_classify(markers),
        markers={k: sorted(set(v)) for k, v in markers.items()},
        tests=sorted(tests, key=lambda t: t.path),
        notes=notes,
    )


def _classify_test_file(name: str) -> Optional[str]:
    if name.startswith(_PYTEST_TEST_RE[0]) and name.endswith(".py"):
        return "pytest"
    if name.endswith(_PYTEST_TEST_RE[1]):
        return "pytest"
    if any(name.endswith(p) for p in _PLAYWRIGHT_PATTERNS):
        return "playwright"
    if any(name.endswith(p) for p in _CUCUMBER_PATTERNS):
        return "cucumber"
    return None


def _classify(markers: Dict[str, List[str]]) -> str:
    has_node = bool(markers.get("node"))
    has_py = bool(markers.get("python"))
    if has_node and has_py:
        return "mixed"
    if has_node:
        return "node"
    if has_py:
        return "python"
    return "unknown"


def recommended_runners(stack: str) -> Tuple[str, str]:
    """Return (api_runner, ui_runner) per ADR 0003 mapping."""
    if stack == "python":
        return ("pytest-httpx", "playwright-ts")
    # node / mixed / unknown default
    return ("playwright-ts", "playwright-ts")


def discovery_to_dict(d: SutDiscovery) -> Dict[str, object]:
    return {
        "sut_root": d.sut_root,
        "stack": d.stack,
        "markers": d.markers,
        "tests": [{"path": t.path, "runner": t.runner} for t in d.tests],
        "notes": list(d.notes),
        "recommended": {
            k: v for k, v in zip(("api_runner", "ui_runner"), recommended_runners(d.stack))
        },
    }
