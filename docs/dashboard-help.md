# Dashboard help

Status: active

Compact in-product guide for the Agentic OS dashboard. The same content is
shipped as `docs/dashboard-help.md` so the help page never drifts from the
docs tree. Quick links — [first run](#first-run-checklist),
[task lifecycle](#task-lifecycle), [inbox](#inbox-quick-start),
[full autonomy](#full-autonomy-primer), [troubleshooting](#troubleshooting),
[legend](#legend).

## First-run checklist

1. **Pick a SUT.** Edit `config/agentic-os.yml` (created by `agentic-os
   init` from `config/agentic-os.yml.example`) — set `sut.root`,
   `sut.kind`, the URLs / compose file you want the OS to probe. Run
   `agentic-os doctor --sut --docker --models` before the first session;
   it surfaces missing tooling before the dashboard does. If you are
   upgrading from an older lab install where the config lived at
   `.qualitycat/agentic-os.yml`, `init` migrates it for you on first run.
2. **Decide on writes.** `dashboard.enable_write_endpoints: false` is the
   safe default; the dashboard renders read-only. There are three unlock
   paths:
   - flip the YAML flag to `true` (persistent),
   - restart with `serve --full` (process-lifetime, set by the CLI),
   - start a full autonomy session (lasts the session; config writes are
     intentionally **not** unlocked here).
3. **Make sure `agentic-os-runtime/` is writable.** The OS keeps the SQLite WAL,
   task specs, runs, evidence and patches under that directory. `agentic-os
   init` creates the layout.
4. **Add the first task.** Either the `/tasks/new` form (dashboard route),
   the [inbox](#inbox-quick-start) tile or `agentic-os task create
   docs/example-task.md`. If your SUT is a public URL (no Docker), see
   the "Online URL SUT" walkthrough in
   [`docs/operator-guide.md`](./operator-guide.md); it carries the
   four `sut.*` keys you need and explains how to write a task spec
   file.

## Task lifecycle

Each task moves through the actions on its detail page (`/tasks/<id>`)
roughly in this order:

1. `analyze` — produces `sut-map.json`, `requirements.md`, `risk-map.md`,
   `candidate-tests.md/json` under `agentic-os-runtime/analysis/<task>/`.
2. `plan` — turns the candidates into `TEST-PLAN.md` under
   `agentic-os-runtime/plans/<task>/`.
3. **Candidates** — review the generated cases (approve / reject /
   needs-decision) before executable code is generated. Decisions are
   persisted; rejected items remain visible for audit.
4. `implement-tests` — writes executable test patches under
   `agentic-os-runtime/patches/<task>/`.
5. `review-gate` — runs the reviewer policy (diff correctness + business
   assumption). It can approve the patch, but does not apply it.
6. `apply-patch` — applies the approved patch to the working tree. Required
   before `run-tests`.
7. `run-tests` — runs the SUT, classifies failures (`product`, `infra`,
   `flaky`, `known-bug`), writes `triage.md`, opens bugs when the gate
   policy says so.
8. `final-gate` — block-merge unless every prior gate has approved,
   `triage.md` exists, and `known-bug` scenarios are still red.

If any step's button is greyed out, see [the writes hint](#first-run-checklist).

## Inbox quick-start

Drop free-form `.md`, `.markdown`, `.txt`, `.docx`, `.pdf` documents into
`./inbox/` or `./pretask/` (or use the **Upload task document** tile on
`/tasks/new`). Then either:

- press **Ingest pending** on the same tile, or
- press **Create task from pending** to synthesize one task from the whole
  bundle, or
- run `agentic-os inbox ingest`.
- run `agentic-os inbox synthesize`.

`ingest` parses each document into its own task spec under
`agentic-os-runtime/task-specs/TASK-…md`. `synthesize` creates one combined
task spec with source references, extracted requirements, endpoints/pages,
known-bug hints and test-data constraints. Successful sources move to
`<intake>/.archive/`; failures move to `<intake>/.failed/` with a sidecar
`*.error.txt`. `.docx` and `.pdf` parsers are optional — install `python-docx`
and `pypdf` to enable them. Markdown specs may declare `Priority: PN` and
`SUT root: <path>` inline; ingest honors those.

## Full autonomy primer

`Start full autonomy` on the home page kicks off a self-driving session:
the OS pulls pending work-items and walks each through analyze → plan →
implement → review-gate → run-tests → final-gate without prompting. While
a session is active:

- task action buttons unlock even when
  `dashboard.enable_write_endpoints=false` (UI polls config every 4 s and
  updates state — the warning text shows which unlock path is active);
- `POST /api/config` and agent / skill writes stay **gated** — autonomy is
  not a sufficient unlock for those, by design.

Min recommended budget: 60 minutes. Stop early with the **Stop** button or
`POST /api/autonomy/stop`. If a step needs sudo, restart the dashboard
with elevated privileges first.

## Troubleshooting

- **Buttons stay greyed out** — read the warning under the buttons; it
  lists every unlock path. The most common cause is forgetting to
  `Start full autonomy` after leaving `enable_write_endpoints=false`.
- **`task list` shows rows that won't open** — the spec file was removed
  out of band. The list flags those with a `MISSING` pill; click **Prune
  missing** on `/tasks` or run `agentic-os task prune-orphans`.
- **`infra_missing_docker` / `infra_missing_compose_file`** — check
  `docs/troubleshooting.md` for the symptom table.
- **`triager-first-check` STOP** — `reports/last-run.json` is stale;
  re-run `run-tests` before triage.

Full table in [`docs/troubleshooting.md`](./troubleshooting.md).

## Legend

Status badges (in order of progression): `queued`, `analyzing`, `planned`,
`implementing`, `reviewing`, `running`, `bug_adjudication`, `blocked`,
`done`, `failed`.

Priority badges: `P0` (critical), `P1` (high), `P2` (default), `P3` (low).

Exit code semantics for run scripts:

- `0` — green run.
- `1` — at least one scenario failed (product / test bug / known-bug
  scenarios still red).
- `2` — infrastructure failure (no SUT, Docker missing, etc.).
- `130` — operator cancelled.
