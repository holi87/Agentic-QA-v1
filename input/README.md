# input/

Host-side **input** directory for the Dockerized Agentic OS. Mounted
**read-only** at `/app/input` in the container, so the OS can never mutate
what you hand it.

Place here material the OS reads **by path reference** (it reads in place;
it does not scan or move these files):

- task specs (Markdown) — reference one as a work item's `spec_path`
  (e.g. `input/login.md`);
- OpenAPI documents, requirement docs, SUT configuration — reference them
  from `config/agentic-os.yml` (`sut.openapi.sources`, `sut.docs.sources`).

This is **not** the `inbox ingest` / `synthesize` drop dir: those scan
`inbox/` / `pretask/` and move processed files, which a read-only mount cannot
do. For the drop-and-ingest flow, mount the host intake dir read-write at
`/app/inbox` instead.

The contract (mount points, modes, ownership, input consumption) is documented
in [`docs/docker-volume-contract.md`](../docs/docker-volume-contract.md). The
Compose wiring lands in issue #353.

Contents are git-ignored; only this `README.md` and `.gitkeep` are tracked.
