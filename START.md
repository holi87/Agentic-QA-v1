# START — quick AgenticOS setup

Cheat-sheet: what to toggle in the config, which flags to start with, where
to look when something breaks. Full reference — `README.md`. Polish version —
`START_pl.md`.

## TL;DR (in order)

```bash
# 1. venv + deps (runtime + dev for pytest)
python3.13 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"

# 2. runtime init (creates agentic-os-runtime/ + config/agentic-os.yml if missing)
./scripts/agentic-os.sh init

# 3. runtime sanity-check (no SUT/Docker — those probes need a configured SUT)
./scripts/agentic-os.sh --json doctor

# 4. dashboard with writes enabled (session-only)
./scripts/agentic-os.sh up --dashboard-only --foreground --full

# 5. UI
open http://127.0.0.1:8765
```

After configuring a SUT (`config/agentic-os.yml` — see "Minimal SUT config"
below), run the full gate:

```bash
./scripts/agentic-os.sh --json doctor --sut --docker --models
```

On a clean checkout that command intentionally fails because the default
`compose_file: docker-compose.yml` is not in the repo — the gate becomes
green once `compose_file` exists (local mode) or is `null` with
`mode: online`.

## From a doc to a candidate plan (intake path)

The golden onboarding path is the `inbox/` + `pretask/` intake — drop
Markdown / text / DOCX / extractable-text PDF documents on disk and the
OS turns them into a structured task spec.

```bash
# Drop one or more documents…
cp my-feature-brief.md inbox/
cp api-spec.md pretask/                 # pretask/ = visible alias for bundles

# …then either synthesize ONE task from the bundle:
./scripts/agentic-os.sh inbox synthesize --title "Feature X regression"
# …or ingest one task per document:
./scripts/agentic-os.sh inbox ingest
```

Same flow is in the dashboard at `/tasks/new` → **Upload task document**.
Full reference + extraction-status semantics (PDF `ok` / `low` / `failed`):
[`docs/operator-guide.md` § "Ingesting external task documents"](docs/operator-guide.md#ingesting-external-task-documents)
([PL](docs/operator-guide_pl.md#ingest-dokumentów-zadań)).

---

## Enabling writes (write endpoints)

The dashboard starts **read-only**. Edit/Save buttons are disabled, write
endpoints return `403`.

Two methods:

### A) Session — `--full` flag (recommended for experiments)

```bash
./scripts/agentic-os.sh up --dashboard-only --foreground --full
```

- In-memory override, gone after the process restarts.
- Does not touch `config/agentic-os.yml`.
- UI badge shows **FULL MODE**.
- Also triggers SUT autostart — add `--no-autostart` if you only want to edit
  the config without spinning up Docker:

```bash
./scripts/agentic-os.sh up --dashboard-only --foreground --full --no-autostart
```

### B) Persistent — YAML

Edit `config/agentic-os.yml`:

```yaml
dashboard:
  host: 127.0.0.1
  port: 8765
  enable_write_endpoints: true   # was false
```

Restart the dashboard:

```bash
./scripts/agentic-os.sh down
./scripts/agentic-os.sh up --dashboard-only --foreground
```

Verify:

```bash
curl -fsS http://127.0.0.1:8765/api/config | jq '.dashboard'
# expect enable_write_endpoints: true
```

> **Note:** `dashboard.host` must stay `127.0.0.1`. Any other host → `ConfigError`.

---

## Minimal SUT config

> **Direction (Wave 17):** the target contract is an **external SUT** —
> the OS runs in Docker and connects to the SUT over web/API URL(s) plus
> an optional DB connection; it never starts the SUT. `mode: local` +
> `autostart` (the Compose-managed local SUT below) is **legacy, pending
> removal** — see
> `ADR-0001`.
> The example below still reflects current `main` behaviour.

File: `config/agentic-os.yml`. Full v2 schema — see `README.md` section
"Configuring a SUT". Bare minimum to boot:

```yaml
sut:
  root: .
  mode: local                        # local (docker-compose) | online (URL)
  compose_file: docker-compose.yml
  compose_project_name: my-app
  autostart: true
  healthcheck:
    command: ["curl", "-fsS", "http://127.0.0.1:3000/health"]
    timeout_seconds: 30
    retries: 10
  test_runner: ./run-tests.sh
  install_shim_allowed: false
  web:
    enabled: true
    url: http://127.0.0.1:3000
  api:
    enabled: true
    url: http://127.0.0.1:3000/api
```

Per-endpoint switches (`web.enabled`, `api.enabled`) gate spec generation:
`false` makes the implementer skip that test family.

## Online mode (no Docker)

For an external SUT (when `mode: online`), the Compose-lifecycle keys (`compose_file`, `compose_project_name`, `autostart`, `install_shim_allowed`) are optional and can be omitted entirely from the config (per Wave 17/ADR-0001). Only the healthcheck command, test runner, and at least one enabled web or API URL are required.

Starting from the "Minimal SUT config" block above, simplify it to:

```yaml
sut:
  root: .
  mode: online
  healthcheck:
    command: ["curl", "-fsS", "https://staging.example.com/health"]
    timeout_seconds: 30
    retries: 10
  test_runner: ./run-tests.sh
  web:
    enabled: true
    url: https://staging.example.com
  api:
    enabled: true
    url: https://staging.example.com/api
```

Healthcheck is still required — it pings `web.url` / `api.url` (or whatever URL you supply in the command) to ensure the external SUT is reachable.

---

## Models

```yaml
models:
  planner:
    provider: claude
    command: ["claude", "--dangerously-skip-permissions", "--model", "opus"]
    role: opus
  implementer:
    provider: claude
    command: ["claude", "--dangerously-skip-permissions", "--model", "sonnet"]
    role: sonnet
  reviewer:
    provider: codex
    command: ["codex", "--sandbox", "danger-full-access", "--ask-for-approval", "never"]
    role: codex
  triager:
    provider: claude
    command: ["claude", "--dangerously-skip-permissions", "--model", "haiku"]
    role: claude
    auto_fire: true
    fallback:
      - { provider: codex, command: ["codex", "--sandbox", "danger-full-access", "--ask-for-approval", "never"], role: codex }
      - { provider: antigravity, command: ["agy", "--dangerously-skip-permissions"], role: gemini }
```

`auto_fire: true` on the triager → runs automatically in the pipeline.
`false` → manual invocation only.

---

## Verifying writes work

If the UI badge says **FULL MODE** but Save is still inert or returns 403:

```bash
# 1. does the running process have the override?
curl -fsS http://127.0.0.1:8765/api/config | jq '.dashboard'

# 2. is an autonomy session active? (also unlocks task-level writes)
curl -fsS http://127.0.0.1:8765/api/autonomy/status | jq '.active'

# 3. event log
curl -fsS http://127.0.0.1:8765/events?limit=20
```

If `/api/config` reports `enable_write_endpoints: false` despite `--full`,
the dashboard process was restarted without the flag (different PID). Kill
it and relaunch with `--full`.

---

## Common gotchas

- **`port 8765 already in use`** → an older dashboard is still alive:
  `lsof -i :8765` + `kill <pid>`.
- **`ConfigError: unknown key`** → typo in YAML or the key isn't on the
  whitelist. Cross-check `scripts/agentic-os/agentic_os/config/`.
- **`credentials.value`** must be an env-var name (e.g. `TEST_USER_TOKEN`),
  never the literal secret.
- **Dashboard shows stale config after YAML edit** → restart the process.
  Config loads once at startup and on-demand for write endpoints.
- **`--full` not honoured on task views** → known glitch: some task screens
  read YAML directly and ignore the in-memory override. For full coverage
  use method B (`enable_write_endpoints: true` in YAML).
- **Two runtime trees** (`.agentic-os/` and `agentic-os-runtime/`) → an
  older checkout left a legacy hidden runtime. `doctor` warns about it.
  Consolidate with `./scripts/agentic-os.sh migrate-runtime`
  (`--dry-run` first to see the plan). Refuses when both have a
  `state.db`; pick the winner manually in that case. Doctor suppresses
  the warning when `runtime.root: .agentic-os` is set in config (the
  legacy path is then an explicit operator choice).

---

## Key files

| File                                          | Contains                                        |
|-----------------------------------------------|-------------------------------------------------|
| `config/agentic-os.yml`                       | Main config (SUT, models, dashboard, gates)     |
| `agentic-os-runtime/state.db`                 | SQLite: events, leases, work-items              |
| `scripts/agentic-os.sh`                       | CLI wrapper (up, down, doctor, task, run, etc.) |
| `scripts/agentic-os/agentic_os/config/`       | YAML schema definitions and validator module     |
| `scripts/agentic-os/agentic_os/routes/`       | Dashboard HTTP server routes and logic          |
| `AGENTS.md`                                   | Full operational rules for agents               |
| `CLAUDE.md`                                   | Hard git workflow rules for Claude              |
