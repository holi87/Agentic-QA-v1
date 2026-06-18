# Dashboard and task-intake analysis

Status: superseded
Superseded-on: 2026-05-22
Superseded-by: operator-guide.md ("Ingesting external task documents" section)
Reason: design-rationale snapshot for the inbox/pretask synthesis flow. The
described decisions (canonical `inbox/`, `pretask/` alias, `inbox synthesize`,
dashboard write-gate mirroring, link-back results) shipped on `main` (PRs
#133, #171). Treat as the rationale record; consult `operator-guide.md` for
the current operator workflow.

Polish translation: [`dashboard-inbox-task-generation_pl.md`](dashboard-inbox-task-generation_pl.md).

## Scope

This pass reviewed the dashboard operator path after the visible runtime-root
migration, with special focus on turning dropped documentation into useful
task specs from both CLI and dashboard.

## Findings

1. The dashboard already had upload + per-file ingest, but the operator still
   had to choose between manual form entry and one task per source document.
   Real intake often starts as a bundle: feature notes, QA constraints,
   public-site URLs, known bugs and acceptance criteria. The existing flow did
   not synthesize those into one coherent task.
2. Disk intake existed only as `inbox/`. The operator asked about a visible
   pre-task staging folder; keeping a `pretask/` alias makes the flow clearer
   without adding a second runtime concept.
3. The inbox tile did not mirror the dashboard write gate before button click.
   Upload/ingest buttons could look actionable while writes were disabled and
   then fail with a 403 response.
4. Ingest results were text-only. Operators could see a created task ID but
   had no direct dashboard link to continue with analyze/plan.
5. `inbox/README.md` still mentioned the old hidden runtime path.

## Implemented decisions

- Keep `inbox/` as the canonical intake directory.
- Add `pretask/` as a tracked, visible staging alias for bundles.
- Keep `inbox ingest` as one-file-one-task for precise task specs.
- Add `inbox synthesize [--title ...]` for one combined task from all pending
  documents in `inbox/` and `pretask/`.
- Expose the same synthesis flow in the dashboard as **Create task from
  pending**.
- Generate synthesized specs deterministically, not as model-only output. This
  keeps the CLI/dashboard path scriptable and available before model execution.
- Preserve model quality gates downstream: the synthesized spec still must go
  through analyze, plan, candidate review and explicit approval before
  executable tests are generated.

## Synthesized task content

The generated task spec includes:

- source document list with relative paths;
- extracted requirement lines;
- detected API endpoints, URLs and page paths;
- known-bug hints;
- test-data, credential and cleanup constraints;
- open questions when surfaces or credentials are missing;
- a warning that candidates must still carry exact assertion, data and cleanup
  metadata before generation.

## Dashboard impact

- `/api/inbox` now reports canonical and supported intake directories.
- `/api/inbox/synthesize` creates one task from the pending bundle.
- `/tasks/new` offers Upload, Ingest pending and Create task from pending.
- Inbox buttons follow the effective write gate (`enable_write_endpoints`,
  `serve --full`, or full-autonomy write unlock).
- Created-task results are rendered as links to `/tasks/<id>`.

## Remaining follow-ups

- Add browser-driven regression for `/tasks/new` once the browser harness is
  available in CI: upload file, synthesize, follow task link, run analyze/plan.
- Consider optional OCR/extraction for scanned PDFs if the operator starts
  using image-only PDFs. Current PDF support intentionally requires extractable
  text through `pypdf`.
