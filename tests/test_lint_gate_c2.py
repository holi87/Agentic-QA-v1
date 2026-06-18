"""Issue #364 — C2 static-analysis lint gate in the generated PW+TS framework.

The issue's body predates ADR-0002 and describes the Java/Maven gate
(Checkstyle + PMD + SpotBugs). Per ADR-0002 the canonical generated-test stack
is Playwright + TypeScript, so the "C2 lint gate" is **ESLint + `tsc --noEmit`**
shipped in the `templates/playwright-ts-framework` scaffold (standards §6 —
"enforced by the C2 lint ruleset"). Every assembled bundle inherits the gate,
and `run-tests.sh` fails the build on a violation.

These tests assert the gate is fully *wired* (config present, scripts declared,
runner invokes the gates, ruleset encodes the standards). That a planted
violation actually fails the build is validated by running ESLint over the
scaffold with Node — captured in the PR (the pytest job has no npm step, like
the #369 e2e validation).
"""
from __future__ import annotations

import json
import subprocess

from agentic_os.standalone import SCAFFOLD_DIR, SCAFFOLD_FILES


def test_eslint_config_is_a_scaffold_file() -> None:
    # Shipped by the generator → every assembled bundle inherits the gate.
    assert "eslint.config.js" in SCAFFOLD_FILES
    assert (SCAFFOLD_DIR / "eslint.config.js").exists()


def test_package_json_declares_lint_and_typecheck() -> None:
    pkg = json.loads((SCAFFOLD_DIR / "package.json").read_text(encoding="utf-8"))
    scripts = pkg["scripts"]
    assert scripts["lint"].startswith("eslint")
    assert "tsc" in scripts["typecheck"] and "--noEmit" in scripts["typecheck"]
    dev = pkg["devDependencies"]
    for dep in ("eslint", "typescript-eslint", "@eslint/js"):
        assert dep in dev, f"missing lint devDependency: {dep}"


def test_eslint_config_encodes_standards_rules() -> None:
    cfg = (SCAFFOLD_DIR / "eslint.config.js").read_text(encoding="utf-8")
    # §6 size & structure limits.
    assert "max-lines" in cfg
    assert "max-lines-per-function" in cfg
    assert "max-depth" in cfg
    assert "no-explicit-any" in cfg
    # §5 — hard waits forbidden; banned via no-restricted-syntax on the call.
    assert "no-restricted-syntax" in cfg
    assert "waitForTimeout" in cfg


def test_run_tests_sh_runs_static_gates() -> None:
    sh = (SCAFFOLD_DIR / "run-tests.sh").read_text(encoding="utf-8")
    # The runner must fail the build on lint/type violations, before tests.
    assert "npm run lint" in sh
    assert "npm run typecheck" in sh
    subprocess.run(["bash", "-n", str(SCAFFOLD_DIR / "run-tests.sh")], check=True)
