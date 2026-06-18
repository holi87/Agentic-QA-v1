# Pretask

Visible staging alias for task-intake bundles.

Drop `.md`, `.markdown`, `.txt`, `.docx`, or `.pdf` files here when you want to
collect documentation, feature notes, bug notes, or acceptance criteria before
creating an Agentic OS task.

Run one of:

```bash
./scripts/agentic-os.sh inbox list
./scripts/agentic-os.sh inbox synthesize
./scripts/agentic-os.sh inbox ingest
```

- `inbox synthesize` creates one task spec from all pending files in `inbox/`
  and `pretask/`.
- `inbox ingest` creates one task per pending file.

The dashboard exposes the same flow at `/tasks/new`.
