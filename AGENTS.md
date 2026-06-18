# AGENTS.md

Instructions for agents working in the AgenticOS Lab repository.

**This file is English-only — no `_pl` twin.** `AGENTS.md` and
`CLAUDE.md` are agent-facing contracts; the canonical and only version
lives here. Do not create `AGENTS_pl.md`.

## Glossary

Two product names appear side by side throughout this repo; they refer to
different layers:

- **Agentic OS** — the orchestration framework that lives in this repo. CLI
  is `agentic-os.sh`; runtime data sits under `.agentic-os/`; the Python
  package is `agentic_os`. Everything operator-facing (dashboard, skills
  framework, init / doctor / run commands, agent role wiring) belongs to
  Agentic OS.
- **QualityCat** — the QA / test-execution domain Agentic OS was built
  for. Lives in the test payloads, not the orchestrator: skill prefix
  `qc-`, `qualitycat-standards/` in contest projects, the generated
  Playwright + TypeScript tests (the `pl.qualitycat` Java packaging was
  inherited from a previous project — canonical stack per
  `ADR-0002`), the
  `agentic_os/qualitycat.py` facade that wraps the contest scripts. When the
  docs mention bug reports, test tag families, or the contest project layout,
  that is QualityCat domain content.

Rule of thumb: orchestration / tooling → Agentic OS; QA standards and
test-execution artifacts → QualityCat.

## Repository purpose

This repo builds Agentic OS: a local pseudo-OS — itself packaged to run in Docker Compose — for creating, running, maintaining and extending web UI tests, REST API tests and bug-aware flows for QualityCat projects.

The system is meant to act as an operational layer on top of a test project:

- it connects to an **external** SUT over web/API URL(s) plus an optional DB connection, injected via env — it **never provisions or starts the SUT** (see ADR-0001; no SUT-in-OS-container, no docker-in-docker);
- it keeps the task queue, phases, decisions, blockers and recovery in SQLite WAL;
- it generates and maintains API/UI tests, reports, bugs and evidence;
- it runs local models / CLIs as executors or review gates;
- it does not modify the SUT, unless a given phase explicitly concerns the fake test SUT inside the lab repo.

## Most important sources

- Contracts: `docs/cli-contract.md`, `docs/runtime-contract.md`, `docs/database-schema.md`.
- Policies: `docs/bug-aware-policy.md`, `docs/severity-policy.md`.
- Operator: `README.md`, `docs/operator-guide.md`, `docs/troubleshooting.md`.

## Documentation language

Documentation in the repo is **English by default**. The Polish
translation lives in a twin file with the `_pl` suffix.

- `START.md` (EN, canonical) ↔ `START_pl.md` (PL, translation).
- `README.md` (EN) ↔ `README_pl.md` (PL).
- The same rule applies to new operator-facing documents: `<NAME>.md` in
  English, `<NAME>_pl.md` in Polish.

**Exceptions — agent-facing contracts kept English-only, no `_pl`
twin:** `AGENTS.md`, `CLAUDE.md`. These files are read by automated
agents (Claude Code, Codex, Gemini) and must not drift between
languages, so we keep a single canonical English version.

When you update a document that has a `_pl` version — sync both, or open
a separate PR with the translation. Drift between the versions on the
same commands, paths and steps is not acceptable.

## Hard git workflow (HARD RULES)

**Branch `main` is read-only for agents.** An agent MUST NOT:

- `git push` (nor `git push --force`) to the `main` / `origin/main` branch;
- `git merge` any branch into `main` locally;
- `git commit` directly on `main`;
- `git rebase` `main` onto anything else;
- bypass hooks (`--no-verify`) or signatures (`--no-gpg-sign`).

Integration into `main` happens exclusively through a Pull Request on
GitHub, after review. An agent may only open a PR (`gh pr create`) and
add comments. A human performs the merge after review.

### Branch per task (TASK ISOLATION)

Each task = its own branch. We do not combine two tasks in one branch,
even when they are small. Naming convention: `task/<short-desc>` or
`phase/NN-<short-desc>` for roadmap phases.

### Before EVERY task starts (REBASE OFF FRESH MAIN)

Before an agent writes anything, it must rebase off a fresh
`origin/main`. No exceptions.

```bash
# 0. Stash local work (if any)
git status
git stash push -u -m "wip-before-task-<slug>"   # only when something is dirty

# 1. Pull the latest main
git fetch origin main
git switch main
git pull --ff-only origin main

# 2. Create a fresh task branch
git switch -c task/<short-desc>
```

If `git pull --ff-only` refuses (local divergence), stop and report to
the operator. Do not resolve with `git reset --hard` or any other
destructive move without explicit consent.

### During the task

- small commits after every verified piece;
- push the branch after every sensible commit or before a context switch;
- do not mix refactor, functionality and documentation in a single commit;
- do not accumulate large work without a commit.

### After the task is finished

```bash
git status
git push -u origin task/<short-desc>
gh pr create --base main --head task/<short-desc> --title "..." --body "..."
```

**Link issues to auto-close (REQUIRED).** The PR body MUST include a
GitHub closing keyword for every issue the PR *fully* resolves — one per
issue: `Closes #NN` (also `Fixes #NN` / `Resolves #NN`). When the PR
merges into `main` (the default branch) GitHub closes those issues
automatically; without the keyword they stay open and must be closed by
hand. Rules:

- One `Closes #NN` line per fully-resolved issue (keywords do not take a
  comma list — `Closes #1, #2` only closes `#1`).
- For an issue the PR advances but does not finish, use a non-closing
  reference (`Refs #NN` / `Part of #NN`) so it stays open.
- Epic / parent issues are referenced (`Refs #NN`), never closed by a
  child PR — they close once all children are done.

The task is ready for merge only after:

- local validation described in the task / phase file;
- a clean `git status` on the branch;
- a review gate, if required;
- an open PR against `main` whose body `Closes` every issue it resolves;
- green CI (if it exists).

**A human performs the merge through the GitHub UI or `gh pr merge`
after review.** An agent never executes a merge into `main`.

## Agent roles (provider-neutral)

Four roles in `config/agentic-os.yml > models.*`:

- **planner** — high-risk decisions, assertion policy, phase plan.
  Default: Claude Opus.
- **implementer** — implementation plumbing, specs, UI, package, verify.
  Default: Claude Sonnet.
- **reviewer** — diff gate (correctness + business assumption, no
  weakening of assertions, argv-only). Default: Codex.
- **triager** — severity + priority assessment for bugs, refining the
  descriptions, cross-checking failed runs. Default: Claude
  (haiku); Codex secondary; Antigravity (`agy --model
  gemini-3.1-pro-high`) as end-of-limit fallback.

Any role can be staffed by any provider — it is enough to change the
`command` and `provider` in the config. The prompts
(`config/prompts/{planner,implementer,reviewer,triager}.md`) are
provider-agnostic. The skills
(`skills/{provider}/qc-{provider}-{role}-{name}.md`) are per-provider
and auto-filtered.

**Reviewer vs triager**: the reviewer assesses the *code and business
assumptions* of a diff. The triager assesses *how serious and how
urgent* a bug is (severity S1-S4, priority P1-P4). The two scopes do
not overlap.

## Autonomy doctrine

Three intra-loop gates that used to require a human are now autonomous
when configured (epics #228 and #234):

| Gate | Old | Autonomous (when flag on) | Recorded |
|---|---|---|---|
| Candidate approval | operator clicks "approve" | planner-coverage-architect emits `decision=generate_now` for the read-only / coverage-floor bucket | `decisions.actor='planner-autopilot'` |
| Per-failure triage | operator YES/NO per failure | triager classifies by fingerprint / OWASP / spec match | `bugs/BUG-NNN.frontmatter.auto_classified=true` |
| Provider routing | operator edits YAML | runtime swaps to next provider on rate-limit / quota / auth signal | NDJSON `provider_failover` event |

The PR → main merge gate stays human (HARD RULES, this file). The
reviewer role (`validate-tests`, `validate-security`, `final-gate`)
keeps biting on every diff regardless of which provider produced it.

Operator-controlled flags (in `config/agentic-os.yml`):

- `autonomy.coverage_floor` — coverage-floor companion emission;
- `autonomy.coverage_architect` — planner autonomous candidate proposal;
- `autonomy.triage_batch` — triager batch classification, no per-failure prompt;
- `models.<role>.auto_fire` — role fires automatically in the loop;
- `models.<role>.fallback` — provider chain.

## Parallel agent orchestration

Scope: the **product's** runtime fan-out (planner probes, implementer
test-families, triage) — not dev-time Claude Code worktrees. Fan out only
independent, read-mostly work; keep correctness-critical steps serialized.

Canonical pattern: **fan-out → synthesize/dedup barrier → single artifact.**
Parallel workers return candidate fragments; one serial barrier merges and
dedups them into the single authoritative artifact (one `TEST-PLAN`, one
patch, one triage batch). Workers never write shared state — the barrier owns
the merge and the write.

| May fan out (independent) | Must stay serialized |
|---|---|
| `explore-sut` probes → one `TEST-PLAN` | review gate, final gate |
| implementer per test family / feature → ordered patch merge | SQLite (WAL) writes — single writer |
| per-failure triage classification | patch apply / approval / decision rows |

Rules:

- **No shared mutable state.** Each agent gets its own scratch / worktree;
  collaborators are passed in, not reached for globally. A worker returns
  data; it does not mutate the DB, the patch, or another worker's files.
- **One writer.** SQLite is single-writer (WAL); concurrent writes corrupt
  state. Parallel workers hand results to the serial barrier, which performs
  the single write under its lease (`runtime.lease_ttl_seconds` /
  `stale_lease_seconds`).
- **Concurrency cap.** Bounded by `runtime.max_parallel_tasks` (default 4);
  fan-out multiplies model calls, so respect provider rate limits and reuse
  the failover chain (Autonomy doctrine) under thrash.
- **Deterministic merge.** The barrier sorts / dedups by a stable key so a
  re-run yields the same artifact (idempotency); conflicts are detected and
  surfaced, never a silent last-write-wins.
- **Gates never fan out.** Review / final gates and any patch approval run
  exactly once, serially, on the merged artifact.

The runtime wiring (planner fan-out #359, implementer fan-out #360,
concurrency controls + WAL safety #361, determinism guardrails #362) builds on
this doctrine.

### Concurrency controls + WAL safety — how it is enforced (#361)

The substrate that makes the table above real. Done carelessly, parallel
agents corrupt WAL state or double-approve a patch — so the controls are
explicit, not emergent.

- **Concurrency caps** — `autonomy/concurrency.py::ConcurrencyController`
  bounds in-flight agents by a global cap (`runtime.max_parallel_tasks`) AND an
  optional per-role cap (`runtime.max_parallel_per_role`); a role with no
  explicit cap inherits the global one, so the global ceiling always binds.
  Acquisition order is role-semaphore-then-global, so a thread never holds the
  global slot while waiting on a role slot — the wait graph has no cycle
  (deadlock-free). `build_concurrency_controller(config)` is the factory the
  fan-out (#359/#360) consumes.
- **Backpressure** — a role is held off only when its *entire* provider
  failover chain is cold (`models.failover.all_providers_cold`), not on a
  single cold provider; the chain still has alive entries until the last one
  trips. Injected into the controller as `backpressure_check` so the substrate
  stays free of config/DB coupling.
- **Gate serialization — why** — the review gate is serialized by the
  per-work-item `work_items.reviewer_lease` token (and that path also performs
  the patch merge). The final gate had no guard: once #360 fans out
  implementers, two agents could both reach `run_final_gate` for one work item
  and write duplicate gate tasks / double-approve. `workflows/stages/leases.py
  ::serialized_gate` closes it; `autonomy/dispatch.py::_autonomy_final_gate`
  wraps the live final gate so the loser refuses (returns a busy `infra`
  outcome) rather than queueing behind the winner.
- **Gate lease store — why the `leases` table, not new columns** — adding
  `work_items.final_gate_lease` columns would need a migration + a
  `SCHEMA_VERSION` bump, which activates the currently-dormant migration 17
  (`ALTER TABLE model_invocations ADD COLUMN work_item_id`, already in
  `schema.sql`) and crashes live DBs with "duplicate column". The existing
  `leases` table needs no migration, its CAS is serialized by `BEGIN
  IMMEDIATE`, and `doctor --repair` already reclaims a lease whose owning
  process died (real pid/host) — so a crashed gate holder auto-recovers. The
  token-equivalent fence is `acquired_at` (ms precision, unique per
  acquisition): release deletes only the exact row it took, so a stale handle
  from a superseded holder cannot clear a live lease.
- **Single writer / WAL** — every DB write goes through
  `storage/db.py::transaction` (`BEGIN IMMEDIATE` + `busy_timeout`), which
  serializes writers across connections; workers never write — they hand
  results to the serial barrier, which owns the single write. The event log
  was the one unguarded surface: `EventLog._update_current_symlink` used
  unlink-then-symlink, and the loser of that race fell back to a plain write
  that *followed* the `current` symlink and truncated the live NDJSON file
  (catastrophic cross-process data loss). It now builds the link under a unique
  temp name and `os.replace`s it onto `current` — an atomic swap that never
  writes through the symlink.

## Agentic OS architecture rules

- The OS runs in Docker (Compose); the SUT is **external** — reached over web/API URL(s) plus an optional DB connection, never started or provisioned by the OS. No SUT-in-OS-container, no docker-in-docker (ADR-0001). The in-repo fake-SUT self-test is exempt: it is a fixture, not a managed SUT.
- Local runtime: no cloud dependencies for the main flow.
- Default interface: `scripts/agentic-os.sh`.
- Runtime data: `.agentic-os/` with SQLite WAL, NDJSON events, logs, patches, worktrees, evidence and backups.
- Configuration: `config/agentic-os.yml`.
- Local dashboard: `localhost:8765`.
- Reports and test artifacts must be produced even when tests end with exit code `1`.
- Known product bugs stay red: `run-tests.sh` returns `1` when `@known-bug` scenarios still fail.
- An infrastructure error returns `2`.
- Anything that is measurable must be scriptable; models cannot be the sole source of truth for execution.

## Test invariants

- Do not weaken assertions without an explicit decision recorded in the system.
- An exact-spec failure must create a bug, evidence and a `@known-bug @bug-NNN` tag — not a green test.
- Every Cucumber scenario must have exactly one `@functional-<area>` tag and at least one lifecycle tag compliant with the standard.
- Name test modules and test functions after the behavior or product surface they verify, not after roadmap phases, waves, issue numbers or implementation chronology.
- Prefer more stable API tests over UI tests, but critical UI flows must have coverage.
- UI tests must collect screenshots/traces on errors when Playwright is used.
- Treat security and accessibility as normal risk areas, not as a late add-on.

## Target structure

Minimal skeleton:

```text
scripts/agentic-os.sh
scripts/agentic-os/agentic_os/
scripts/agentic-os/templates/
scripts/assertion-guard.py
config/agentic-os.yml
config/prompts/
docs/
```

Runtime generated locally:

```text
.agentic-os/
reports/
bugs/
evidence/
```

Commit framework code and documentation. Ignore runtime, cache, backups
and old source materials per `.gitignore`.

## Pre-push validation

Pick the smallest sensible set of commands for the change:

- documentation: check links/paths manually and run `git diff --check`;
- shell: `bash -n <script>`;
- Python: `python -m py_compile <files>` or unit tests;
- frontend/dashboard: run locally and check in a browser when the phase touches UI;
- runner: confirm that reports are produced before returning non-zero.

Do not skip hooks and do not use destructive git commands without
explicit consent.
