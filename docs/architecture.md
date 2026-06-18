# Architecture

Status: active. Authored for issue #293 and verified against the codebase at
`scripts/agentic-os/agentic_os/`. This is the canonical architecture map. The
caveman-compressed block near the end is what gets injected into agent prompts
(`models._invoke_attempt`); keep this human-readable doc as the source of truth
and update the block in the same commit.

## Module map

The runtime is the `agentic_os` package under `scripts/agentic-os/`.

- **Entry / orchestration**: `cli.py` (operator CLI), `orchestrator.py`
  (work-item + run lifecycle), `autonomy.py` (unattended session loop),
  `scheduler.py` (cron-style schedule firing), `queue.py` (queue ordering +
  token estimates).
- **Dashboard**: `routes/dashboard_server.py` (loopback HTTP server; `server.py`
  is a thin alias), `dashboard.py` (view builders), `templates/`.
- **Models**: `models/__init__.py` — `invoke_model` (public) → `_invoke_attempt`
  (single chokepoint that composes the prompt and runs the provider CLI),
  `models/providers/` (per-provider parsing + envelope suffix). Skills and the
  architecture context are spliced here.
- **Prompt assembly**: `skills.py` (`compose_prompt` prepends per-role skills),
  `architecture_context.py` (injects the compressed architecture summary).
- **Workflows**: `workflows/` — analyze, plan, implement-tests, review-gate,
  run-tests, final-gate steps; `runner.py` wraps `runtime/subprocess.py` with
  run records + manifests.
- **Subprocess boundary**: `runtime/subprocess.py` (the only path to external
  commands; argv-only, curated PATH, env allowlist, log redaction),
  `security.py` (`require_safe_argv`, `resolve_repo_path`, redaction).
- **SUT**: `sut_lifecycle.py` (compose up/down, healthcheck), `sut_discovery.py`,
  `sut_repo.py`, `exploratory.py` (baseline runner), `crawler*.py`.
- **Quality flows**: `gates.py` (review/final gates), `learnings.py` (advisory
  learnings + decay), `decisions.py`, `results.py` / `triage_classifier.py`
  (bug triage), `qualitycat.py` (QA script facade), `coverage_review.py`.
- **Storage**: `storage/db.py` (WAL connection + migration runner),
  `storage/schema.sql`, `budgets.py` (token aggregation + `estimate_tokens`),
  `events.py` (append-only event log), `paths.py` (`RuntimePaths`).
- **Generators / planning**: `generators/` (API + UI test generation),
  `plan_v2.py`, `test_planning.py`, `openapi.py`.

## Work model: project → work_item → phase → task

- A **work_item** is one unit of QA work (created from a task spec, inbox
  ingest, or autonomy). Issue #288 will add an addressable **project** layer
  above work_items; today the runtime is single-SUT.
- **phases** are the seeded pipeline stages; a work_item advances through step
  phases `analyze → implement → review → triage` (see `_ROLE_TO_STEP_PHASE`).
- **tasks** / **runs** record individual executions; `runs` rows pair with
  subprocess logs and manifests. `leases` guard concurrent work-item ownership.
- Dependencies between work_items live in `work_item_deps`; produced files in
  `work_item_artifacts`.

## Runtime DB tables

SQLite, WAL, `storage/schema.sql`, `SCHEMA_VERSION = 13` (migrations in
`storage/db.py`). Core tables:

`work_items`, `work_item_deps`, `work_item_artifacts`, `tasks`, `runs`,
`phases`, `leases`, `events`, `event_offsets`, `model_invocations`,
`model_transcripts`, `learnings`, `decisions`, `blockers`, `bugs`, `evidence`,
`test_results`, `assertion_changes`, `autonomy_sessions`, `provider_cooldowns`,
`schedules`, `session_bookmarks`, `schema_migrations`.

## Model-role wiring

Four roles, each a primary provider + failover chain (`config/agentic-os.yml`):

| Role | Primary | Failover | Step phase |
|---|---|---|---|
| planner | claude/opus | codex, antigravity/gemini | analyze |
| implementer | claude/sonnet | codex, antigravity/gemini | implement |
| reviewer | codex | claude/sonnet, antigravity/gemini | review |
| triager | claude/haiku | codex, … | triage |

All four flow through `models._invoke_attempt`, which: composes the prompt
(architecture context + skills + base task + envelope suffix), runs the
provider CLI via `runtime.subprocess`, redacts the input, and parses the
provider envelope. Failover re-resolves provider-specific skills.

## Gate / learnings / memory flows

- **Gates** (`gates.py`): `static_review_gate` on diffs; reviewer output parsed
  by `parse_reviewer_invocation` → `parse_gate_output` (strict APPROVE/REJECT
  envelope); `final_gate` / `evaluate_final_gate` decide release readiness.
  Assertion changes require a decision row; exact-spec failures open bugs.
- **Learnings** (`learnings.py`): `record_learning` writes advisory rows;
  `decay_learnings` ages them by `decayed_weight`; `provider_quality_scores`
  and `flaky_subjects` inform routing. Writes are best-effort — a learning
  failure never breaks the host flow. Injection into prompts is issue #287.
- **Memory**: per-project RAG memory is issue #289 (planned); it will index
  session summaries / transcripts / bugs and share the injected-context budget
  with the architecture context and learnings.

## Injected agent context

`architecture_context.py` reads the compressed block below, bounds it to a
token budget (`prompt_context.architecture_budget_tokens`, default 600), and
`models._invoke_attempt` prepends it ahead of skills. Injection is best-effort:
a missing/malformed block emits `architecture.injection_failed` and the call
proceeds without it. The block must stay free of secret-shaped literals
(`security.py` redaction would otherwise mangle it).

**Token delta** (`budgets.estimate_tokens`, ~4 chars/token): the full
human-readable doc is ~1700 tokens; the injected compressed summary is ~291
tokens (~315 with the prompt wrapper) — an ~83% reduction versus shipping the
whole doc, comfortably inside the 600-token default budget. The remaining
budget headroom is left for the #287 learnings and #289 memory injections so
the three do not stack past a bounded prompt-context budget.

<!-- inject:architecture-summary:start -->
Agentic OS = local QA agent runtime (package `agentic_os`). Work unit =
work_item; advances phases analyze -> implement -> review -> triage. Roles:
planner (claude/opus), implementer (claude/sonnet), reviewer (codex), triager
(claude/haiku); each has failover chain. All model calls go through
`models._invoke_attempt`: prompt = arch-context + per-role skills + task +
envelope suffix; provider CLI runs via `runtime.subprocess` (argv-only,
curated PATH, env allowlist, log redaction). State = SQLite WAL
(`storage/schema.sql`, version 13): work_items, runs, tasks, phases, leases,
events, model_invocations, learnings, decisions, bugs, evidence, test_results,
autonomy_sessions. Gates (`gates.py`): reviewer emits strict APPROVE/REJECT
envelope; final_gate decides release; assertion changes need a decision row;
exact-spec failures open bugs. Learnings (`learnings.py`) advisory + decayed,
best-effort, never block host flow. Dashboard = loopback-only
(`routes/dashboard_server.py`), unsafe methods need host+origin+token. SUT
commands run unsandboxed but without provider credentials. Output contract:
emit the role envelope; do not re-summarize loaded skills.
<!-- inject:architecture-summary:end -->
