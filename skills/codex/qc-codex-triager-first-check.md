---
name: qc-codex-triager-first-check
description: "Cross-check the newest test run output against existing bugs/, identify failures NOT yet in bugs/, classify each as test-bug vs app-bug vs spec-ambiguity vs infra-fail, propose severity (S1-S4) + priority (P1-P4) + OWASP/ISO tag, then prompt user per-failure for the log decision."
---

# Skill: qc-codex-triager-first-check

## Communication

${include_preamble}

## Standards

Test-run artifacts and code structure follow the canonical conventions — read the source, do not restate it: `docs/standards/playwright-ts-standards.md` (§1 layering, §2 page objects, §3 typed clients, §5 assertions/web-first waiting, §7 tags, §8 security). Failures arrive in `reports/last-run.json` produced from the Playwright JSON reporter; evidence is the Playwright HTML report + trace.

## When to use

- AFTER implementer-verify has completed and classified failures inline — first-check is a post-hoc sweep over the consolidated `reports/last-run.json`, NOT a parallel pass.
- Before final submit — last triage gate (user MUST invoke; NEVER auto-run).
- After importing a new test run from CI / another machine where verify did not run locally.
- NOT during implementation slices (verify already classifies inline; running first-check then would race on the same artifacts).
- NOT when `reports/last-run.json` is stale / missing (re-run tests first).

## What to do

1. Read `reports/last-run.json` (STOP if missing — instruct user to run `./run-tests.sh` or `./scripts/extract-last-run.sh`). Read `bugs/README.md` index + every `bugs/BUG-NNN-<slug>.md` frontmatter.
2. Classify each failure entry:
   - MATCH (tag `@known-bug @bug-NNN` AND matching file exists) → expected red, SKIP. Log as EXPECTED.
   - DRIFT (tag without file OR file without tag) → integrity drift; recommend remove tag (test bug) OR create file (real bug).
   - NO-MATCH → run `triager-link-duplicates` first; if it returns DUPLICATE, append `@known-bug @bug-NNN` instead of creating a bug. Only on NEW_DISTINCT → NEW POTENTIAL BUG → continue.
3. For each NO-MATCH failure: open the failing test at `spec_path:line` (the `tests/<area>.spec.ts` `test(...)` body), read its actions + `{ tag: [...] }`; trace each assertion to the typed client / page-object method it calls; open spec section (`solution/ARCHITECTURE.md`, `requirements.md`, OpenAPI section).
4. Classify (LLM judgment): APP-BUG (assertion mirrors spec AND app violates), TEST-BUG (assertion wrong: wrong expected value, wrong endpoint, locator stale, env config), SPEC-AMBIG (default to Info-severity bug + Open Question), or INFRA-FAIL (SUT down, port conflict, network → NOT a bug; recommend re-run).
5. Recommend severity (S1 Critical / S2 High / S3 Medium / S4 Low / Info per bug-reporting.md matrix) + priority (P1-P4 = severity × frequency × business value) + likelihood (H/M/L) + OWASP/ISO mapping (cite OWASP API Top 10 item if security tag or auth/permission/role/token in the test title; ISO 25010 dimension for functional). REQUIRED even if spec absent — explain rationale.
6. Decide per unreported failure. Behavior depends on `autonomy.triage_batch` in `config/agentic-os.yml`:

   **`autonomy.triage_batch=false` (default — preserves current behavior):** ask user PER unreported failure (one prompt per failure, never batch): YES log with recommended severity (default) | YES log with override severity | NO mark as test-bug (fix later) | NO skip / re-run needed.

   **`autonomy.triage_batch=true` (issue #232 — autonomous classification):** call `agentic_os.triage_classifier.classify_failure(failure, known_bugs=load_known_bugs('bugs/'))` and execute the returned action. Rule table:

   | Condition | Action | Reviewer-still-bites |
   |---|---|---|
   | Status + endpoint + assertion text match an existing `bugs/BUG-NNN-*.md` fingerprint within Levenshtein ≤ 5 | `append_known_bug` — append `@known-bug @bug-NNN` to the test's `{ tag: [...] }`; no new bug | Reviewer `validate-features` still rejects orphan `@known-bug` tags |
   | `@security-*` tag failed AND OWASP mapping unambiguous (`@owasp-api1` + status 200 from unauthorized call) | `auto_create_bug` at S1/P1; `auto_classified: true` in frontmatter; `found_by: triager-autopilot` | Operator can downgrade via dashboard; immutability not assumed |
   | Assertion mirrors spec (`spec_path` cite) AND failure deterministic (`run_count >= 2`) AND severity ≤ S2 | `auto_create_bug` at recommended severity; `auto_classified: true`; loop continues | Reviewer `final-gate` re-checks before PR |
   | Infrastructure failure (port / DNS / connection refused / docker not running / SUT down) | `skip_infra` — NO bug; emit re-run request; never block on operator | n/a |
   | Anything else (SPEC-AMBIG, severity S1 with low-confidence mapping, conflicting fingerprints) | `queue_operator` — fall-through; loop continues with other failures | Operator decision recorded |

   **Recorded actor:** every auto-created bug carries `triager-autopilot` in `bugs/BUG-NNN-*.md` frontmatter (`found_by: triager-autopilot`, `auto_classified: true`) so the dashboard chip surfaces autonomous decisions for fast operator audit. Severity overrides by operator always honor over autonomous classification.
7. On YES → `./scripts/new-bug.sh "<test title>"` creates `bugs/BUG-NNN-<slug>.md` skeleton + `evidence/BUG-NNN/`. Edit frontmatter: severity, likelihood, component (API/UI/DB/Messaging/Infra), owasp, iso25010, found_by, test (spec file + test title from the Playwright JSON report), scenario (`spec_path::test_title`). Fill body: Steps to Reproduce (from the test actions), Expected per spec (cite section), Actual (paste error + stack_head), Evidence pointers (HTML report + trace), Impact, Suggested Fix.
8. Append `@known-bug @bug-NNN` to the test's `{ tag: [...] }` in the `.spec.ts`.
9. Run `./scripts/new-bug.sh --reindex` to refresh `bugs/README.md`.
10. Write `reports/triage-<ISO>.md` audit log: per-failure classification, recommendation, user decision, action taken.

## Output

- Zero or more new `bugs/BUG-NNN-<slug>.md` files (one per CONFIRMED app-bug, user YES only).
- Updated `bugs/README.md` index.
- `reports/triage-<ISO>.md` — full audit (Outcomes table, Drift Findings, Expected Reds, Recommendations).
- Git commits per logged bug.
- Final summary report: examined failures count, expected reds, drift cases, new bugs logged, test-bug flagged, infra-fails.

## Example

A per-failure classification table in `reports/triage-<ISO>.md`:

```markdown
| Failure | Classification | Severity | Action |
|---|---|---|---|
| orders.spec.ts:18 cancel returns 200 | APP-BUG (assertion mirrors spec) | S2/P2 | auto_create_bug BUG-014 |
| login.spec.ts:9 stale locator | TEST-BUG | — | fix test |
| port 8080 connection refused | INFRA-FAIL | — | skip_infra, re-run |
```
