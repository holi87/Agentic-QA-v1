from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROVENANCE_FILENAME = re.compile(r"^test_(?:step2_)?phase\d|^test_wave\d")


def test_test_module_filenames_describe_behavior_not_delivery_phase() -> None:
    phase_named = sorted(
        path.name
        for path in ROOT.glob("test_*.py")
        if PROVENANCE_FILENAME.search(path.name)
    )

    assert phase_named == []


def test_test_function_names_are_unique_across_modules() -> None:
    locations: dict[str, list[str]] = defaultdict(list)

    for path in ROOT.glob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                locations[node.name].append(f"{path.name}:{node.lineno}")

    duplicates = {name: refs for name, refs in locations.items() if len(refs) > 1}
    assert duplicates == {}
