# output/

Host-side **output** directory for the Dockerized Agentic OS. The OS writes
its operator-facing artifacts here through **read-write** bind mounts:

- `output/reports/` &larr; `/app/reports` — run reports (HTML/JSON),
- `output/bugs/` &larr; `/app/bugs` — bug artifacts,
- `output/evidence/` &larr; `/app/evidence` — evidence handoff copies.

Private runtime state (`state.db` + WAL) is **not** here — it lives on a named
volume to avoid SQLite locking issues over Docker Desktop file sharing. The
self-contained generated framework is surfaced here by issues #369/#373.

The contract (mount points, modes, uid/gid 10001 ownership) is documented in
[`docs/docker-volume-contract.md`](../docs/docker-volume-contract.md). The
Compose wiring lands in issue #353.

Contents are git-ignored; only this `README.md` and `.gitkeep` are tracked.
