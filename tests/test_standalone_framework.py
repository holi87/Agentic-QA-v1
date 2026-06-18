"""Issue #369 — standalone, human-runnable Playwright + TypeScript suite.

The OS emits a self-contained npm project (scaffold + generated `.spec.ts`)
that a human runs outside the OS with `./run-tests.sh`. Per ADR-0002 the stack
is npm/Playwright, not Maven/pom.xml.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agentic_os.standalone import (
    SCAFFOLD_DIR,
    SCAFFOLD_FILES,
    assemble_standalone_framework,
)


@dataclass(frozen=True)
class _FakeTest:
    relative_path: str
    content: str


_SPEC = _FakeTest(
    relative_path="tests/api/cand-1-demo.spec.ts",
    content=(
        "import { test, expect, request } from '@playwright/test';\n"
        "test('demo', async () => { expect(1 + 1).toBe(2); });\n"
    ),
)


# ---- scaffold template integrity (no Maven/pom.xml; npm/Playwright) -------

def test_scaffold_has_all_npm_playwright_files() -> None:
    for name in SCAFFOLD_FILES:
        assert (SCAFFOLD_DIR / name).exists(), f"missing scaffold file: {name}"
    # The stack is npm/Playwright — there must be NO Maven artifacts.
    assert not (SCAFFOLD_DIR / "pom.xml").exists()
    assert not (SCAFFOLD_DIR / "mvnw").exists()


def test_scaffold_package_json_declares_playwright() -> None:
    pkg = json.loads((SCAFFOLD_DIR / "package.json").read_text(encoding="utf-8"))
    dev = pkg.get("devDependencies", {})
    assert "@playwright/test" in dev
    assert pkg["scripts"]["test"].startswith("playwright test")


def test_scaffold_run_tests_sh_is_valid_bash() -> None:
    script = SCAFFOLD_DIR / "run-tests.sh"
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    # Must drive npx playwright, not mvn.
    assert "playwright test" in text
    assert "mvn" not in text
    subprocess.run(["bash", "-n", str(script)], check=True)


# ---- assembler -----------------------------------------------------------

def test_assemble_writes_scaffold_and_specs(tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    manifest = assemble_standalone_framework(output_dir=out, tests=[_SPEC])

    for name in SCAFFOLD_FILES:
        assert (out / name).exists()
    spec = out / "tests/api/cand-1-demo.spec.ts"
    assert spec.exists()
    assert spec.read_text(encoding="utf-8") == _SPEC.content

    assert (out / "manifest.json").exists()
    assert manifest["runner"] == "playwright-ts"
    assert manifest["tests"] == [{"relative_path": "tests/api/cand-1-demo.spec.ts"}]
    # A real npm project — package.json is valid JSON with Playwright.
    pkg = json.loads((out / "package.json").read_text(encoding="utf-8"))
    assert "@playwright/test" in pkg["devDependencies"]


def test_assemble_is_idempotent(tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    assemble_standalone_framework(output_dir=out, tests=[_SPEC])
    # Second run over the same dir must not raise and must keep the bundle whole.
    assemble_standalone_framework(output_dir=out, tests=[_SPEC])
    assert (out / "package.json").exists()
    assert (out / "tests/api/cand-1-demo.spec.ts").exists()


def test_assemble_with_no_tests_still_produces_runnable_scaffold(tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    manifest = assemble_standalone_framework(output_dir=out, tests=[])
    assert (out / "package.json").exists()
    assert (out / "run-tests.sh").exists()
    assert manifest["tests"] == []
