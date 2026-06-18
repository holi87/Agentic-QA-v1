# Docker networking & external-SUT reachability

Status: active

How the Agentic OS, running in a container, reaches the **external** SUT
(web/API endpoints + an optional database) and where SUT secrets come from.
The OS never starts the SUT. See
`ADR-0001`
and the volume side of the contract in
[`docs/docker-volume-contract.md`](docker-volume-contract.md).

This document defines the **contract**. The Compose *wiring* that enforces it
— `extra_hosts`, environment passthrough, the published dashboard port —
lands in `docker-compose.yml` (issue #353).

## Reachability

Inside the container, `127.0.0.1` / `localhost` is the **container itself**,
not the host. Reach targets accordingly:

- **SUT on the host** → use `host.docker.internal`.
  - macOS / Windows (Docker Desktop): `host.docker.internal` resolves to the
    host automatically.
  - Linux: Compose must add `extra_hosts: ["host.docker.internal:host-gateway"]`
    so the name resolves (wired in #353).
- **Remote / staging SUT** → an env-injected public URL
  (`https://staging.example.com`). Resolves via normal DNS; no host mapping.

## In-container healthcheck

`sut.healthcheck.command` (e.g. `curl -fsS <url>`) runs **from inside** the
container, so its URL must be reachable from the container's network
namespace — a host SUT uses `host.docker.internal`, a remote SUT uses its
public hostname. The same rule applies to `sut.web.url` / `sut.api.url`,
which the runtime injects into the test runner as `UI_BASE_URL` /
`API_BASE_URL` (issue #92): they are dialed from inside the container too.

`agentic-os doctor` validates that `healthcheck.command` is a non-empty argv
list; the actual probe is executed at runtime.

## Optional database reference

`sut.db` is a **reference-only** block — never an inline secret:

```yaml
sut:
  db:
    ref_type: env     # env | file | none
    value: SUT_DB_PASSWORD # env var name (or, for ref_type: file, a repo-relative path)
```

The referenced value holds the full DSN
(`driver://user:pass@host:port/dbname`). Driver, host, port, user and
database name live **inside** the DSN, not as separate config keys — this
keeps connection details in one operator-controlled secret and avoids secret
sprawl in config. For a database on the host, the DSN host is
`host.docker.internal`; for a remote database, its hostname.

The OS performs no database connection today; the block is a declared
reference resolved at runtime by whatever consumes it (operator scripts /
generated tests). A built-in in-container DB check would own its own
consumption of this reference.

## Secrets — nothing baked, refs only, redaction

- **Nothing baked into the image.** The `Dockerfile` ships only the example
  config and supporting files; the live `config/agentic-os.yml` and all
  secrets are provided at runtime via environment, a mounted config, or
  mounted secret files (#353 wires the passthrough).
- **The SUT test runner never receives the operator's model credentials.**
  `scrub_provider_credentials()` strips `ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `GEMINI_API_KEY`, … from the
  child environment before the SUT-supplied runner sees it (issue #291).
- **Refs only — no inline secrets.** The config validator rejects a
  `sut.db` / `sut.credentials` whose `ref_type` is not `env` / `file` /
  `none`; an inline DSN/token fails validation (exit code 2).
- **Log redaction.** Test-runner stdout/stderr/status lines pass through
  `redact_sensitive_text()`, which scrubs:
  1. the **values** of env vars whose **name** matches a secret keyword
     (`api_key`, `apikey`, `secret`, `password`, `passwd`, `token`,
     `bearer`, `credential`, `access_key`, `private_key`, `client_secret`);
  2. secret-shaped literals — `Authorization:` headers, `bearer …`,
     `sk-…`, `ghp_…`/`ghs_…`, AWS `AKIA…` keys, PEM private-key blocks,
     `password=…`.

  Independently, `env_hash()` (dry-run fingerprint) feeds the full
  environment through SHA-256; it stores only the digest, never raw values.

### Redaction limitation — name secret env vars with a keyword

Env-value redaction keys off the variable **name**. A DSN held in a variable
named `DATABASE_URL` (no keyword in the name) is **not** auto-redacted if it
surfaces in runner output. Until redaction honours config-declared secret
refs (follow-up **#385**), name secret-bearing variables with a recognized
keyword so they are covered — e.g. `SUT_DB_PASSWORD`, `SUT_DB_CREDENTIAL`,
`*_SECRET`, `*_TOKEN`. The `credentials` example uses `TEST_USER_TOKEN`
(contains "token"), which **is** covered today.

## Worked examples

### (a) SUT on the host

```yaml
sut:
  root: .
  mode: online
  healthcheck:
    command: ["curl", "-fsS", "http://host.docker.internal:3000/health"]
    timeout_seconds: 30
    retries: 10
  test_runner: ./run-tests.sh
  web:
    enabled: true
    url: http://host.docker.internal:3000
  api:
    enabled: true
    url: http://host.docker.internal:3000/api
```

On Linux, Compose adds `extra_hosts: ["host.docker.internal:host-gateway"]`
(#353); on Docker Desktop it already resolves.

### (b) SUT on a staging URL

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

### (c) SUT + database

```yaml
sut:
  root: .
  mode: online
  healthcheck:
    command: ["curl", "-fsS", "http://host.docker.internal:3000/health"]
    timeout_seconds: 30
    retries: 10
  test_runner: ./run-tests.sh
  web:
    enabled: true
    url: http://host.docker.internal:3000
  db:
    ref_type: env
    value: SUT_DB_PASSWORD   # env var name; holds the secret/DSN. Keyword-named so logs redact it.
```

Provide the secret at runtime via the Compose env passthrough (#353), e.g.
`SUT_DB_PASSWORD=postgres://user:pass@host.docker.internal:5432/app`. For a
remote database use its hostname in place of `host.docker.internal`.
