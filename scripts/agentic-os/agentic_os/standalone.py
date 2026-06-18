"""Assemble a standalone, human-runnable Playwright + TypeScript suite (#369).

The OS generates `.spec.ts` files (generators/api.py, generators/ui.py) and
collects them into a patch artifact. This module produces the *human-runnable*
counterpart: a self-contained npm project (the
``templates/playwright-ts-framework`` scaffold) with the generated specs dropped
into ``tests/``. A human copies the output to any machine with Node.js and runs
``./run-tests.sh`` — no Agentic OS, no Docker, no Java/Maven.

ADR-0002 sets the canonical generated-test stack to Playwright + TypeScript, so
the standalone framework is npm/Playwright (``package.json`` + ``npx playwright``),
not Maven/`pom.xml`.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Protocol


SCAFFOLD_DIR = Path(__file__).resolve().parent.parent / "templates" / "playwright-ts-framework"

# Scaffold files copied verbatim into every assembled bundle.
SCAFFOLD_FILES = (
    "package.json",
    "playwright.config.ts",
    "tsconfig.json",
    "eslint.config.js",
    "run-tests.sh",
    "README.md",
    ".gitignore",
)


class _GeneratedTestLike(Protocol):
    relative_path: str
    content: str


def assemble_standalone_framework(
    *,
    output_dir: Path,
    tests: Iterable[_GeneratedTestLike],
) -> Dict[str, Any]:
    """Write the scaffold + generated specs into ``output_dir``.

    Returns a manifest describing the bundle. ``output_dir`` is created if
    missing; existing scaffold files are overwritten so re-assembly is
    idempotent. Each test's ``relative_path`` (e.g. ``tests/api/x.spec.ts``) is
    honored verbatim under ``output_dir``.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    for name in SCAFFOLD_FILES:
        src = SCAFFOLD_DIR / name
        if not src.exists():
            raise FileNotFoundError(f"scaffold file missing: {src}")
        dst = output_dir / name
        shutil.copy2(src, dst)

    entries: List[Dict[str, str]] = []
    for gen in tests:
        rel = str(gen.relative_path)
        target = output_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(gen.content, encoding="utf-8")
        entries.append({"relative_path": rel})

    # Issue #371 — the final operator-handoff artifact: a human-readable guide
    # shipped with the runnable bundle. Generated (not a static scaffold file)
    # so the CLI regenerator (#372) can fill it from a run manifest.
    from .run_guide import write_run_guide_html

    guide = write_run_guide_html(output_dir)
    guide_name = guide.name

    manifest = {
        "version": "1.0",
        "kind": "standalone-playwright-ts-framework",
        "runner": "playwright-ts",
        "scaffold_files": list(SCAFFOLD_FILES),
        "guide": guide_name,
        "tests": entries,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest
