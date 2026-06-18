from __future__ import annotations

from pathlib import Path

from agentic_os.assertions import guard_files
from agentic_os.storage import init_db


def test_assertion_guard_blocks_removed_assertion_and_records_it(tmp_path: Path) -> None:
    before = tmp_path / "before.py"
    after = tmp_path / "after.py"
    before.write_text("def test_api():\n    assert response.status_code == 201\n", encoding="utf-8")
    after.write_text("def test_api():\n    print(response.status_code)\n", encoding="utf-8")
    db_path = tmp_path / ".agentic-os" / "state.db"

    result = guard_files(
        before_path=before,
        after_path=after,
        file_path="tests/test_api.py",
        db_path=db_path,
    )

    conn = init_db(db_path)
    try:
        row = conn.execute(
            "SELECT file_path, classification, status, decision_id FROM assertion_changes;"
        ).fetchone()
        blocker = conn.execute("SELECT severity, source, status FROM blockers;").fetchone()
    finally:
        conn.close()

    assert result.ok is False
    assert result.blocked == 1
    assert row["file_path"] == "tests/test_api.py"
    assert row["classification"] == "weakened"
    assert row["status"] == "blocked"
    assert row["decision_id"] is None
    assert blocker["severity"] == "P1"
    assert blocker["source"] == "assertion-guard"
    assert blocker["status"] == "open"


# ---- issue #367: static Playwright+TS anti-pattern detectors -----------------
# Beyond assertion *weakening*, the guard statically rejects newly-introduced
# hard waits (§5), hardcoded URLs and hardcoded secrets (§8). Detection scans
# lines present in `after` but NOT in `before`, so it flags what the patch
# introduces — and must NOT fire on the generators' own env-injected output.


def _guard(tmp_path: Path, before_src: str, after_src: str):
    before = tmp_path / "before.ts"
    after = tmp_path / "after.ts"
    before.write_text(before_src, encoding="utf-8")
    after.write_text(after_src, encoding="utf-8")
    return guard_files(before_path=before, after_path=after, file_path="tests/x.spec.ts")


def test_guard_blocks_introduced_hard_wait(tmp_path: Path) -> None:
    result = _guard(
        tmp_path,
        "test('x', async ({ page }) => {\n  await expect(page).toHaveURL(/ok/);\n});\n",
        "test('x', async ({ page }) => {\n  await page.waitForTimeout(1000);\n"
        "  await expect(page).toHaveURL(/ok/);\n});\n",
    )
    assert result.ok is False
    assert result.blocked >= 1
    assert any("hard wait" in c.reason.lower() for c in result.changes)


def test_guard_blocks_introduced_hardcoded_url(tmp_path: Path) -> None:
    result = _guard(
        tmp_path,
        "const base = process.env['API_BASE_URL'];\n",
        "const base = 'https://prod.example.com/api';\n",
    )
    assert result.ok is False
    assert result.blocked >= 1
    assert any("url" in c.reason.lower() for c in result.changes)


def test_guard_blocks_introduced_hardcoded_secret(tmp_path: Path) -> None:
    result = _guard(
        tmp_path,
        "const token = process.env['SUT_API_TOKEN'];\n",
        "const token = 'Bearer sk_live_abc123def456ghi';\n",
    )
    assert result.ok is False
    assert result.blocked >= 1
    assert any("secret" in c.reason.lower() for c in result.changes)


def test_guard_does_not_flag_env_injected_generator_output(tmp_path: Path) -> None:
    # Real generator shape (#364/#365): env-injected URL + Bearer from env.
    # The guard MUST NOT flag this — false positives would block the OS's own
    # compliant output.
    generated = (
        "const API_BASE_URL = process.env[\"API_BASE_URL\"];\n"
        "const _ctx = await request.newContext({ baseURL: API_BASE_URL,\n"
        "  extraHTTPHeaders: { 'Authorization': `Bearer ${process.env[\"SUT_API_TOKEN\"] ?? ''}` } });\n"
        "await page.goto(new URL(targetPage, process.env[\"UI_BASE_URL\"]).toString());\n"
        "expect(_resp.status()).toBe(201);\n"
    )
    result = _guard(tmp_path, "", generated)
    assert result.ok is True, [c.reason for c in result.changes]
    assert result.blocked == 0


def test_guard_ignores_preexisting_anti_pattern(tmp_path: Path) -> None:
    # A hard wait already present in `before` is not introduced by this patch.
    src = "test('x', async ({ page }) => {\n  await page.waitForTimeout(500);\n});\n"
    result = _guard(tmp_path, src, src + "// trailing comment\n")
    assert not any("hard wait" in c.reason.lower() for c in result.changes)
