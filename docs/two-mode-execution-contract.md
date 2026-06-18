# Two-mode execution contract

Status: active

Generated suites run in **two additive modes** — not either/or:

- **Mode A — inside the OS container.** The Agentic OS runs the suite itself
  (the Node + Playwright image, issue #389) as part of a work item.
- **Mode B — standalone on the host.** A human runs the self-contained npm
  bundle the OS assembles (issue #369,
  [`standalone.py::assemble_standalone_framework`](../scripts/agentic-os/agentic_os/standalone.py))
  with `./run-tests.sh` — no Agentic OS, no Docker, no Java/Maven.

**The contract:** the *same* suite, the *same* files, the *same* config surface
run in both modes. Switching modes changes **only environment values** — never
the test code, never a config file edit. This is the acceptance bar for #370.

## The env-var contract (identical in both modes)

Both the in-container runner and the standalone `run-tests.sh` read the SUT
location and credentials from the environment. The generators emit specs that
read exactly these names:

| Env var | Consumed by | Notes |
| --- | --- | --- |
| `API_BASE_URL` | API specs (`api/*.spec.ts` → `process.env['API_BASE_URL']`, Playwright `request` baseURL) | injected by the runtime in-container (issue #92); exported by the human standalone |
| `UI_BASE_URL` | UI specs (`ui/*.spec.ts` → `page.goto(new URL(path, UI_BASE_URL))`) | same in both modes |
| *credentials env* (e.g. `SUT_API_TOKEN`) | API auth — `Authorization: Bearer ${process.env['…']}` | the env var **name** is in the source, never the secret value (see [`standards/playwright-ts-standards.md`](standards/playwright-ts-standards.md) §8) |
| `SUT_DB_*` (optional, e.g. `SUT_DB_PASSWORD`) | typed DB client / DSN | `ref_type: env`; keyword-named so logs redact it (see networking contract) |

A spec self-skips or fails loudly when a required env var is absent — it never
falls back to an inline default.

## What changes between modes: only the env values

| Env var | Mode A — in-container, host SUT | Mode B — standalone on host | Remote / staging (either mode) |
| --- | --- | --- | --- |
| `API_BASE_URL` | `http://host.docker.internal:3000/api` | `http://localhost:3000/api` | `https://staging.example.com/api` |
| `UI_BASE_URL` | `http://host.docker.internal:3000` | `http://localhost:3000` | `https://staging.example.com` |

The files under test are byte-identical across the columns; only the exported
values differ.

## Networking caveat (in-container runs)

In **Mode A**, a SUT on the host is reached via **`host.docker.internal`**, not
`localhost` (the container's `localhost` is its own namespace). On macOS /
Windows (Docker Desktop) this resolves automatically; on Linux, Compose adds
`extra_hosts: ["host.docker.internal:host-gateway"]`. Full reachability rules,
the healthcheck namespace, and the database reference live in
[`docker-networking-contract.md`](docker-networking-contract.md) — this contract
references it rather than duplicating it.

In **Mode B**, the suite runs on the host directly, so a host SUT is plain
`localhost`; a remote SUT uses its public URL in both modes.

## Running each mode

- **Mode A** — the OS drives `run-tests.sh` inside the image during the work
  item; URLs come from `sut.*` config translated into `API_BASE_URL` /
  `UI_BASE_URL` (issue #92).
- **Mode B** — export the env vars and run the assembled bundle:

  ```bash
  export API_BASE_URL=http://localhost:3000/api
  export UI_BASE_URL=http://localhost:3000
  export SUT_API_TOKEN=…          # only if the suite needs auth
  ./run-tests.sh                  # npm ci → playwright test
  ```

---

_Part of Wave 17 EPIC D (standalone execution & operator handoff). The env
contract and `host.docker.internal` rules are reframed to the Playwright + TS
stack per ADR-0002 (the issue's `SUT_WEB_URL`/`SUT_API_URL` names predate the
runtime's `UI_BASE_URL`/`API_BASE_URL`, issue #92). Auto-emitting the standalone
bundle during a run, and surfacing it with a how-to-run guide, is #371–#373._
