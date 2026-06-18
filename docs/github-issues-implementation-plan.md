# GitHub Issues Implementation Plan

Status: active

Source snapshot: GitHub open issues for `holi87/agentic-web-testing`, fetched
on 2026-05-27 at 10:57 UTC. Open PRs at snapshot time: none.

## Purpose

This plan turns the current open GitHub issues into an implementation order.
It is a routing document, not a replacement for the issue bodies. Each issue or
substantial sub-scope still gets its own task branch and PR against `main`.

## Current open issues

| Issue | Area | Priority | Planning verdict |
|---|---|---:|---|
| #295 `[bug][dashboard] Full autonomy stays idle with online SUT and budget endpoint import deadlock` | dashboard, autonomy, budget imports | P1 | First release-blocker. Fix before deeper autonomy features. |
| #291 `[security] Hardening round 2 - dashboard auth, file traversal, SUT sandbox` | security, dashboard, subprocess boundary | P0 guardrail | Must land before expanding dashboard write/autonomy surfaces. |
| #293 `[docs] Architecture context for agents - authored doc + token-efficient injection` | docs, prompt context, token budget | P1 enabler | Enables shared context budget for #287 and #289. |
| #288 `[arch] Project abstraction layer - addressable projects over flat work_items` | storage, config, work item scoping | P2 enabler | Required by #289; isolate before semantic memory. |
| #287 `[autonomy] Learnings producers + prompt injection (#273 follow-up)` | learnings, gates, prompt hints | P1/P2 enabler | Split producers from prompt injection; injection coordinates with #293/#289. |
| #289 `[autonomy] Per-project RAG memory - semantic recall of project history across sessions` | memory, search, prompt context | P2 feature | Blocked by #288; shares injection budget with #293/#287. |
| #290 `[epic][autonomy] True unattended operation - autonomy round 2` | autonomy epic | P1/P2 epic | Implement as child branches after #295 and the memory/learnings enablers. |
| #296 `[bug][dashboard] Menu layout jumps between subpages` | dashboard UI | P2 | Independent polish; do after security/online-autonomy blockers unless it blocks demos. |
| #292 `[epic][refactor] Decompose oversized modules` | cleanup/refactor | P2/P3 epic | Do last or one module at a time after behavior is pinned by tests. |

## Dependency order

```text
#295 online SUT autonomy + budget import deadlock
  -> unblocks truthful dashboard full-autonomy behavior

#291 security hardening
  -> must precede wider dashboard write/autonomy usage

#293 architecture context and shared prompt budget
  -> coordinates prompt injection for #287 and #289

#288 project abstraction
  -> required by #289 per-project memory

#287 learnings producers
  -> feeds #290 proactive skill failover and planner hints

#289 per-project RAG memory
  -> feeds next-session context and #290 deeper autonomy

#290 true unattended operation children
  -> consumes #287/#289 and fixes remaining operator intervention points

#296 dashboard nav stability
  -> independent P2 UI stabilization

#292 decomposition
  -> behavior-preserving cleanup after blockers are covered
```

## Implementation waves

### Wave 1 - online autonomy release blocker (#295)

Recommended branch: `task/295-online-autonomy-budget`

Scope:

- Move `budget_status()` and its SQLite aggregation helpers out of
  `agentic_os.models.__init__` into a lightweight module such as
  `agentic_os/budgets.py`.
- Update CLI `budget show` and dashboard `/api/budget/status` to import the
  lightweight module.
- Add an online-SUT branch to the empty-queue autonomy path. When
  `sut.mode: online` and `sut.web.enabled: true`, discovery must use the saved
  web URL rather than only `sut.root`.
- If full task synthesis is intentionally deferred to #290, record a precise
  blocking state instead of repeating useful-looking `idle:awaiting-task`.

Acceptance:

- Repro with `https://qualitycat.com.pl` no longer loops forever without either
  actionable work or a deterministic block reason.
- Concurrent `/api/budget/status` polling does not import the full
  `agentic_os.models` package and does not raise `_DeadlockError`.
- Tests cover online config persistence, empty-queue online behavior, and
  lightweight budget imports.

Suggested checks:

```bash
.venv/bin/python -m pytest \
  tests/test_autonomy_preflight.py \
  tests/test_exploratory_baseline.py \
  tests/test_autonomy_cli_controls.py \
  tests/test_config_v2_dashboard_api.py
./run-tests.sh
```

### Wave 2 - security hardening (#291)

Recommended branch: `task/291-dashboard-security-hardening`

Scope:

- Add an unsafe-method auth guard for dashboard `POST` / `PUT` / `DELETE`.
  `enable_write_endpoints` continues to gate feature availability; the new
  guard establishes caller identity for local unsafe writes.
- Audit `/files/` serving so every served path resolves under an allowed root
  and private runtime state remains blocked.
- Minimize or explicitly document inherited environment for SUT subprocesses.
  Do not weaken `require_safe_argv`.

Acceptance:

- Unauthenticated unsafe dashboard methods are rejected; authenticated writes
  still work in local/full mode.
- `../` and absolute-path `/files/` payloads are rejected by regression tests.
- SUT subprocess trust boundary and feasible minimal-env behavior are
  documented.

Suggested checks:

```bash
.venv/bin/python -m pytest \
  tests/test_dashboard_origin_guard.py \
  tests/test_dashboard_server.py \
  tests/test_config_v2_dashboard_api.py \
  tests/test_generator_and_subprocess_security.py
./run-tests.sh
```

### Wave 3 - shared architecture context (#293)

Recommended branch: `task/293-architecture-context`

Scope:

- Author `docs/architecture.md` and `docs/architecture_pl.md`, verified against
  code: module map, runtime DB tables, project/work_item/phase/task model,
  model role wiring, gate/learnings/memory flows.
- Add one prompt-context assembly path that can inject compressed architecture
  context and later share budget with #287 learnings and #289 memory.
- Measure and document token delta for raw vs compressed context.

Acceptance:

- English and Polish docs are synced on commands, paths, and model.
- Agent prompts carry bounded architecture context without hiding or replacing
  provider-specific skills.
- Tests prove context injection is deterministic and budget-limited.

Suggested checks:

```bash
.venv/bin/python -m pytest \
  tests/test_model_invocation_wrappers.py \
  tests/test_skill_loader_splice.py \
  tests/test_operator_guide_doc_references.py
git diff --check
```

### Wave 4 - addressable projects (#288)

Recommended branch: `task/288-project-abstraction`

Scope:

- Add migration v14 with a `projects` table and nullable `work_items.project_id`
  backfilled to a default project from the current SUT config.
- Resolve active project from CLI/config and scope work items, sessions,
  learnings, and future memory reads by `project_id`.
- Preserve single-SUT runtime behavior as the zero-config path.

Acceptance:

- Existing runtime DB migrates to one default project with no behavior change.
- A second project can be registered and work items remain isolated.
- #289 has a stable `project_id` boundary to attach memory rows.

Suggested checks:

```bash
.venv/bin/python -m pytest \
  tests/test_runtime_guards.py \
  tests/test_work_item_artifacts.py \
  tests/test_work_item_dependency_link.py \
  tests/test_session_history.py \
  tests/test_learnings.py
./run-tests.sh
```

### Wave 5 - learnings producers and prompt hints (#287)

Recommended branch: `task/287-learnings-producers`

Scope:

- Add producer detectors for `flaky`, `skill_failure`, and `coverage_gap`.
- Keep all writes advisory and best-effort; a learning write failure must not
  break the host flow.
- Inject relevant learnings into planner/implementer prompts through the shared
  context budget from #293, and emit `learning.consulted`.

Acceptance:

- Each producer writes a learning on a real detection event with a focused test.
- Planner/implementer prompts carry bounded hint blocks.
- Existing store/read/decay tests keep passing.

Suggested checks:

```bash
.venv/bin/python -m pytest \
  tests/test_learnings.py \
  tests/test_results_bug_classification.py \
  tests/test_coverage_architect_autopilot.py \
  tests/test_codex_review_gate_hardening.py \
  tests/test_test_plan_schema_review.py
./run-tests.sh
```

### Wave 6 - per-project RAG memory (#289)

Recommended branch: `task/289-project-rag-memory`

Prerequisite: #288 merged. Coordinate final injection budget with #293 and #287.

Scope:

- Add `agentic_os/memory.py` with SQLite FTS5 indexing for session summaries,
  model transcripts, bugs, decisions, and learnings.
- Add `memory build` and `memory query <text>` CLI commands scoped to active
  project.
- Inject compressed prior-context snippets at session start or prompt assembly.

Acceptance:

- `memory build` indexes one project's history and does not mix projects.
- `memory query` returns ranked relevant snippets.
- Prompt/session context stays within the configured budget.

Suggested checks:

```bash
.venv/bin/python -m pytest \
  tests/test_session_summary.py \
  tests/test_reasoning_transcripts.py \
  tests/test_learnings.py \
  tests/test_autonomy_cli_controls.py
./run-tests.sh
```

### Wave 7 - true unattended operation children (#290)

Recommended branch pattern: `task/290-<child-slug>`

Implement as separate child PRs:

1. Automated task synthesis from requirements, failures, crawl output, and
   coverage gaps.
2. Per-phase checkpoint and resume so a phase retry does not restart the whole
   work item.
3. Deterministic decision auto-completion for mechanically decidable gates.
4. Proactive skill-failover recovery on reviewer REJECT, consuming
   `skill_failure` learnings from #287.
5. Cost prediction and early abort before a session budget is exhausted.

Acceptance:

- Empty-queue full autonomy either creates bounded actionable work or records a
  precise deterministic block.
- Mid-phase failures resume from the failed phase only.
- Mechanical decisions auto-resolve with auditable decision rows.
- Skill/provider retry is bounded and does not bypass reviewer gates.
- Cost prediction emits an early block before budget exhaustion.

Suggested checks:

```bash
.venv/bin/python -m pytest \
  tests/test_autonomy_preflight.py \
  tests/test_autonomy_step_outcome.py \
  tests/test_queue_policy_ordering.py \
  tests/test_provider_failover.py \
  tests/test_autopilot_verifications.py
./run-tests.sh
```

### Wave 8 - dashboard navigation stability (#296)

Recommended branch: `task/296-dashboard-nav-stability`

Scope:

- Centralize dashboard shell/nav markup or generate it from one helper.
- Remove page-specific nav width, spacing, active-state, or sticky-position
  differences.
- Add screenshot or structural tests that compare nav x/y/width/height across
  representative pages in desktop and narrow widths.

Acceptance:

- Menu/nav position does not shift between dashboard subpages.
- Active, hover, and focus states do not resize links or neighboring content.
- Desktop and narrow screenshots show one consistent shell pattern.

Suggested checks:

```bash
.venv/bin/python -m pytest \
  tests/test_dashboard_ui_contracts.py \
  tests/test_dashboard_screenshots.py \
  tests/test_dashboard_browser_regression.py
```

### Wave 9 - oversized-module decomposition (#292)

Recommended branch pattern: `task/292-split-<module>`

Scope:

- Split one target module per PR only: `routes/dashboard_server.py`, `cli.py`,
  `workflows/_legacy.py`, `gates.py`, `inbox.py`, or `autonomy.py`.
- Keep public import paths stable or update all call sites in the same PR.
- Treat this as behavior-preserving cleanup; no feature work in the same branch.

Acceptance:

- Full suite is green after each split.
- No operator-facing behavior changes unless explicitly documented in the
  issue/PR.
- Review diff is small enough to audit one module boundary at a time.

Suggested checks:

```bash
./run-tests.sh
git diff --check
```

### Wave 10 - wire invoke_model into the autonomous pipeline (#308)

Recommended branch: `task/308-wire-invoke-model`

Prerequisite for re-scoping #290 children 3-5. The autonomy loop is
deterministic artifact orchestration today: `analyze_work_item`,
`plan_work_item`, and `implement_tests_for_work_item` build artifacts without
calling a model, `invoke_model` has zero non-test callers, and the only writer
of `model_invocations.cost_usd` (`models._invoke_attempt`) is reachable only
through it, so runtime session cost is always zero. Children 3-5 of #290 each
assume an in-process model step that does not exist.

Scope:

- Wire `invoke_model` into the model-driven steps (at minimum plan generation,
  ideally analyze) so they run in-process and record `model_invocations` rows
  with `session_id`, `tokens_in/out`, and `cost_usd`.
- Preserve the provider chain and `_rank_chain_by_quality` failover semantics.
- Do not regress the deterministic orchestration shipped by #290 children 1-2.

Acceptance:

- An autonomous session records `model_invocations` rows (cost/tokens/session)
  for its model-driven steps.
- `budget_status` reflects non-zero session cost during a real autonomous run.
- After this lands, re-scope #290 children 3 (deterministic decision
  auto-completion), 4 (proactive skill-failover), and 5 (cost prediction +
  early abort) against the now-real in-process surface.

Suggested checks:

```bash
.venv/bin/python -m pytest \
  tests/test_model_invocation_wrappers.py \
  tests/test_autonomy_step_outcome.py \
  tests/test_session_summary.py
./run-tests.sh
```

## Round 2 — Waves 11-16 (full unattended autonomy + redesign)

Waves 1-10 (#295, #291, #293, #288, #287, #289, #290, #296, #292, #308) are
merged. The repo is a working Agentic OS but `docs/rc-readiness-analysis.md`
scores it 58/100 — BLOCK for external RC. Round 2 closes the gap to the stated
end goal: an autonomous system, controllable from CLI **and** dashboard, with
metrics and monitoring, that builds and **accumulates** automated tests for one
SUT — per queued tasks when tasked, exploratory when idle — and never just sits
on a block reason. The dashboard then gets a full visual redesign.

Each wave is a GitHub milestone with an epic issue and (for the early waves)
child issues. Order is dependency-first: behavior, then accumulation, then the
operator pipeline, then observability, then CLI/config, then the visual layer.

### Wave 11 — Online-only exploratory autonomy (epic #311)

Recommended branch: `task/317-online-exploratory-default`

Directly fixes the reported online-only block:
`idle:blocked — online web URL was crawled from sut.web.url, but empty-queue
task synthesis is deferred to issue #290`. Root cause: `autonomy.py:737-744`
records a block while the exploratory path (`_maybe_exploratory_baseline`,
autonomy.py:1146) stays gated on `autonomy.exploratory_baseline` (off by
default).

Children:
- #317 — default-on exploratory baseline for an online-only empty queue (P1).
- #318 — exploratory baseline events + preflight/doctor messaging.
- #321 — `[bug]` `task.html`/`decision.html` lack the canonical shell+nav
  (#296 left these bare detail views untouched; the menu still drops/jumps on
  `/task/<id>`). Filed here as a Wave 11 quick win; absorbed by Wave 16.

Acceptance: an online-only operator who sets only `sut.web.url` gets a growing
exploratory suite without queuing a task or editing flags; no spurious
`idle:blocked`.

### Wave 12 — Test accumulation per SUT across runs (epic #312)

Recommended branch pattern: `task/312-<child-slug>`

The flagship behavior. Accumulate one SUT's suite across runs: new tests for new
tasks, an exploratory delta when idle, never a duplicate. Builds on Wave 11 and
the model wiring from #308; reuses #287 learnings and #289 per-project RAG.

Children:
- #319 — coverage ledger (persistent per-SUT covered-surface record).
- #320 — idempotent generation gated on the ledger.
- (to file when the wave starts) per-task delta; exploratory delta.

Acceptance: re-running on an unchanged SUT adds zero duplicate specs; a new
route/task adds exactly the new coverage; the ledger explains what is covered.

### Wave 13 — End-to-end RC test pipeline (epic #313)

Recommended branch pattern: `task/313-<child-slug>`

Closes RC-readiness gaps 1, 2, 5, 6. Makes candidate → approve → generate → run
→ bug one proven flow on CLI and dashboard: operator-grade candidate promotion,
exact-spec failure → bug+evidence, fallback-assertion hardening
(`needs_operator_decision`, not silent green), and one RC smoke test on a fake
SUT.

### Wave 14 — Metrics & monitoring cockpit (epic #314)

Recommended branch pattern: `task/314-<child-slug>`

Unified observability. `/api/metrics` rollups (tests created/run, pass/fail per
surface, coverage delta, session cost/tokens — real after #308, failover rate,
block-reason distribution, time per phase), one cockpit view evolving the
earlier dashboard specs (#193/#195/#196/#202), and an optional
Prometheus/JSON export.

### Wave 15 — CLI completeness & truthful config readiness (epic #315)

Recommended branch pattern: `task/315-<child-slug>`

Closes RC-readiness gaps 4 and 7. Orchestrator daemon `up`/`down`,
`logs --follow`, `install-shim`, a truthful `doctor` against the canonical
config and all model roles, and a runnable sample-SUT scaffold.

### Wave 16 — Dashboard full redesign (epic #316)

Recommended branch pattern: `task/316-<child-slug>`

Scheduled last on purpose — redesigning before behavior and metrics stabilize
wastes rework. Modern, professional, readable, effective visual layer **on top
of** the views already shipped (#244, #246, #247, #266-#270, #191-#212): design
system + offline tokens (#200), one app shell for every page incl. the bare
detail views, per-view redesign, responsive + a11y, refreshed screenshot
baselines.

## Global guardrails

- Use one branch and PR per issue or substantial child scope.
- Start every branch from fresh `origin/main`; never push or merge directly to
  `main`.
- Keep doc twins synchronized for any operator-facing docs.
- Do not weaken test assertions. Exact-spec failures stay red and go through the
  bug-aware flow.
- Dashboard/frontend changes need browser or screenshot verification.
- Storage migrations need compatibility tests against an existing runtime DB.
- Autonomy changes must emit auditable events for every autonomous decision,
  block, failover, and budget stop.

## Recommended immediate next branch

Waves 1-10 are merged. Start Round 2 with `task/317-online-exploratory-default`
(#317, Wave 11). It directly removes the reported online-only `idle:blocked`
state — the system currently refuses to do anything useful when configured with
only a web URL — and turns "no tasks" into exploratory test generation, which is
the precondition for the Wave 12 accumulation behavior. Pair it with the cheap
nav-consistency bug #321 in the same wave.
