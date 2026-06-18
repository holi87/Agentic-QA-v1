# Docker volume contract

Status: active

How the host filesystem and the Agentic OS container exchange data when the
OS runs from Docker (`docker compose up`). The OS runs in the container; the
SUT is external. See
`ADR-0001`
for the architecture decision and
[`docs/runtime-contract.md`](runtime-contract.md) for the canonical runtime
directory layout this contract maps onto the host.

This document defines the **contract** (which directories are mounted, the
read/write mode, bind vs named volume, and ownership). The concrete
`volumes:` stanza lives in `docker-compose.yml` (issue #353). End-to-end
verification — a task spec dropped into host `input/` producing artifacts in
host `output/` — is validated once Compose wires these mounts (#353).

## Container layout

The image (`Dockerfile`, issue #352) fixes the in-container layout:

- `WORKDIR /app` — the OS repo root (`paths.repo_root`). The CLI shim resolves
  the repo root from its own location; there is no git in the container.
- Runtime user `agentic`, **uid/gid 10001** (non-root). Everything the
  container writes is owned by 10001.
- Public artifacts are written under the repo root: `/app/reports`,
  `/app/bugs`, `/app/evidence`.
- Private runtime state lives under `/app/agentic-os-runtime` (`state.db` +
  SQLite WAL, `events/`, `logs/`, `worktree/`, `leases/`, …).

## Mounts

| Host (compose default) | Container | Mode | Type | Carries |
| --- | --- | --- | --- | --- |
| `./input` | `/app/input` | **ro** | bind | task specs (Markdown), OpenAPI, requirement docs, SUT config — consumed **by path reference** (see below) |
| `./output/reports` | `/app/reports` | **rw** | bind | HTML/JSON run reports |
| `./output/bugs` | `/app/bugs` | **rw** | bind | bug artifacts |
| `./output/evidence` | `/app/evidence` | **rw** | bind | evidence handoff copies |
| `agentic-os-runtime` | `/app/agentic-os-runtime` | **rw** | **named volume** | `state.db` + WAL, events, logs, worktrees — survives restarts |

The operator puts material into host `input/` (mounted read-only so the OS can
never mutate what it was given) and reads results from host `output/`.

### Input consumption — by path reference, not by inbox scan

`input/` is **read-only**, so it carries inputs the OS reads **in place, by
path**:

- a task spec referenced as the work item's `spec_path` (e.g.
  `spec_path: input/login.md`, resolved under the repo root by
  `resolve_repo_path`);
- OpenAPI / requirement docs / SUT config referenced from
  `config/agentic-os.yml` (`sut.openapi.sources[].value`,
  `sut.docs.sources[].value`, …) as `input/<file>`.

It is **not** scanned by `inbox ingest` / `inbox synthesize`: those commands
read the runtime intake dirs `inbox/` and `pretask/` (`INTAKE_DIRNAMES`) and
**move** each processed file into `.archive/` / `.failed/` — a mutation that a
read-only mount cannot perform. To use the drop-and-ingest UX in a container,
mount the host intake dir **read-write** at `/app/inbox` (separate from the
read-only `input/`); do not point `inbox ingest` at `input/`.

### Why a named volume for runtime state

`agentic-os-runtime/` holds the SQLite database and its WAL. On Docker
Desktop (macOS/Windows) a *bind* mount crosses the VirtioFS / gRPC-FUSE
boundary, where SQLite file locking is unreliable and can corrupt the WAL. A
**named volume** lives on the Linux VM's native filesystem — no locking
hazard — and still survives `docker compose down`/`up`. Bind mounts are used
only for the human-readable artifacts (`reports/`, `bugs/`, `evidence/`) and
the read-only `input/`.

## Configuration constraint — keep artifact paths relative

The default `paths:` block resolves relative to the repo root:

```yaml
paths:
  reports: reports   # -> /app/reports in the container
  bugs: bugs         # -> /app/bugs
  evidence: evidence # -> /app/evidence
```

Parts of the runtime read these config keys; other code writes the same
artifacts via a hardcoded `repo_root / "reports"` (gates, task synthesis).
Both resolve to the **same** `/app/reports` only while the path keys stay at
their relative defaults. A mounted `config/agentic-os.yml` **must not**
override `paths.reports` / `bugs` / `evidence` with absolute paths — doing so
splits the config-honoring readers from the hardcoded writers and the single
bind mount stops capturing both. Leave them relative; the mounts line up.

## Generated framework

The acceptance criterion also names "the generated test framework" as a host
output. Today the framework (its `pom.xml`, `run-tests.sh`, `src/test/…`)
materializes at the repo root (`/app`). Making it a self-contained,
human-runnable directory is issue #369, and surfacing it (with reports and
evidence) into `output/` with links from the run guide is #373. Until those
land, the **guaranteed** operator-facing outputs of this contract are
`reports/`, `bugs/`, and `evidence/`; the framework path is finalized by
#369/#373.

## Ownership and permissions

The container writes as uid/gid **10001**. The host targets must be writable
by that id, or writes fail with `Permission denied`:

- **macOS / Windows (Docker Desktop):** the file-sharing layer maps ownership
  for bind mounts, so host `input/` and `output/` are normally writable as-is.
  The named volume is owned inside the Linux VM and needs no host action.
- **Linux:** bind-mounted host dirs keep their real uid/gid. Either
  `chown -R 10001:10001 output` (and make `input/` readable by 10001) before
  `docker compose up`, or run the service as the host user (`user:` in
  Compose, #353) so the container writes with the operator's id.

The named-volume runtime state is initialized owned by 10001 on first run, so
it needs no manual ownership step on any platform.

## Host directory scaffolding

The repository ships empty `input/` and `output/` directories — and the
`output/reports`, `output/bugs`, `output/evidence` **bind-source subdirs** —
each with a `.gitkeep` (and `input/`/`output/` also a short `README.md`); their
runtime contents are git-ignored. Shipping the subdirs means they exist on a
fresh checkout, so the documented `chown -R 10001:10001 output` covers them and
Docker Compose does not create them root-owned. They are the compose defaults
for the bind mounts above — the operator fills `input/` and collects results
from `output/`.
