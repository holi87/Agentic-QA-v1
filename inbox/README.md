# Inbox

Drop free-form task documents (`.md`, `.markdown`, `.txt`, `.docx`, `.pdf`) into
this directory and Agentic OS will materialize them as task specs under
`agentic-os-runtime/task-specs/`.

Use `pretask/` when you want a separate visible staging folder for larger
documentation bundles. The CLI and dashboard read both `inbox/` and `pretask/`.

## How it runs

- CLI: `scripts/agentic-os.sh inbox list` to inspect pending files,
  `scripts/agentic-os.sh inbox ingest` to process one task per document, or
  `scripts/agentic-os.sh inbox synthesize` to create one synthesized task from
  every pending document.
- Dashboard: `/tasks/new` → "Upload task document" tile (Upload + Ingest pending
  + Create task from pending buttons). Uploaded files land here; **Ingest
  pending** runs one-task-per-document, while **Create task from pending** creates
  one synthesized task brief from the whole bundle.

## What ingest does

For each pending file:

1. Parses the file into UTF-8 text.
   - `.md` / `.markdown` — used as-is; H1 becomes the task title.
   - `.txt` — first non-empty line becomes the title; full text becomes
     "Expected behavior".
   - `.docx` — requires `python-docx`. Paragraphs are joined with double newlines.
   - `.pdf` — requires `pypdf`. Page text is joined with double newlines.
2. Creates a work item via `create_work_item_from_payload` (same code path used
   by the `task create` CLI command and the `/tasks/new` form).
3. Moves the source file to `<intake>/.archive/<stem>-<UTC-ts>.<ext>`.

`inbox synthesize` parses the same file types, extracts source references,
requirements, endpoints/pages, known-bug hints, and test-data constraints, then
creates one task spec. Successful sources move to `.archive/` under their
original intake folder (`inbox/` or `pretask/`).

On failure (unsupported extension, empty document, parser missing, validation
error) the source file is moved to `<intake>/.failed/` with a sidecar
`<name>.error.txt` describing the cause.

## Notes

- Limit per file: 4 MiB.
- `inbox/.archive/`, `pretask/.archive/`, `.failed/` dirs and any document
  dropped into either intake folder are gitignored (`.gitkeep` and `README.md`
  are the only tracked files).
- `.docx` and `.pdf` parsers are optional dependencies; install
  `python-docx` and `pypdf` to enable them. Without those deps `.md` / `.txt`
  still work.
