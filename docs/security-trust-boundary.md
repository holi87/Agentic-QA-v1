# Security trust boundary

Status: active. Covers the local dashboard server, runtime-file serving, and
SUT subprocess execution. Authored for issue #291 (hardening round 2);
round-1 mechanisms (`security.py`, host-rebinding guard) are assumed in place.

The Agentic OS dashboard is a **loopback-only, single-operator** tool. Every
control below is scoped to that model. None of it makes the dashboard safe to
expose on a shared network — keep it bound to `127.0.0.1`.

## 1. Dashboard unsafe-method authentication

`do_POST` / `do_PUT` / `do_DELETE` all pass through `_enforce_unsafe_request`,
which applies three checks in order:

1. **Host header** must name a loopback host (DNS-rebinding defence, issue #148).
2. **Origin / Referer** must be loopback or absent (CSRF defence, issue #148).
3. **`X-Agentic-Token`** must equal the server token (issue #291), compared
   with `hmac.compare_digest`.

### Token lifecycle

- Resolved once per server in `_load_or_create_dashboard_token`:
  `AGENTIC_DASHBOARD_TOKEN` env → existing `<runtime_root>/.dashboard_token`
  (mode `0600`) → a fresh `secrets.token_urlsafe(32)` persisted at `0600`.
- The server embeds the token in every rendered HTML page (a
  `<meta name="agentic-dashboard-token">` tag plus a one-line `window.fetch`
  shim that attaches the header to same-origin POST/PUT/DELETE).

### What the token defends, and what it does not

| Threat | Defended? | By what |
|---|---|---|
| Cross-origin browser page POSTing to localhost (CSRF) | Yes | Origin guard **and** token (the attacker page cannot read the response body cross-origin, so it cannot learn the token) |
| Process owned by a **different OS user** | Yes | `0600` token file is unreadable to other users |
| Process owned by the **same OS user** that can `cat .dashboard_token` | **No** | This is the OS user-account boundary; out of scope for a local single-operator tool |
| Network attacker | N/A | Server binds loopback only |

If the token file cannot be written (read-only runtime dir), the in-memory
token still gates the running process; persistence is the only thing lost.

`enable_write_endpoints` is a **feature** flag (which endpoints exist), not an
**identity** flag. The token is the identity check and is always enforced when
a token is provisioned.

## 2. Runtime-file serving (`/files/`)

`_serve_runtime_file` serves read-only artifacts and is the only path that
maps a URL onto the filesystem. Hardening (issue #291):

- The suffix is routed through `security.resolve_repo_path`, which rejects NUL
  bytes, absolute paths, `~` expansion, and `..` escapes, and guarantees the
  resolved target stays under `repo_root`.
- The target must additionally fall under one **explicit allow-list** of
  served roots (reports, bugs, analysis, plans, task specs, runs, evidence,
  patches, subprocess logs, support bundles). Every allow-list entry is
  `.resolve()`-d so the containment check compares fully-resolved paths on
  both sides — a symlinked root cannot smuggle a path past the check.
- Anything that fails any check returns `404` (never `403`), so the route does
  not confirm the existence of paths outside the allow-list.

Regression payloads (`../`, `reports/../../secret`, `/etc/passwd`,
`%2Fetc%2Fpasswd`, symlink escape, non-whitelisted repo path) are covered in
`tests/test_dashboard_server.py`.

## 3. SUT subprocess trust boundary

SUT-supplied commands (`sut.healthcheck.command`, `sut.test_runner`, compose
up/down, the exploratory baseline runner) are **untrusted custom binaries**.
They run on the operator's machine, unsandboxed, with the operator's file
permissions. Agentic OS does not containerise them; the trust boundary is:

- **Argv only.** Every command is validated by `security.require_safe_argv`
  (no shell strings, no `sh -c`, no NUL bytes) and resolved against a curated
  PATH. This is unchanged and must not be weakened.
- **No model credentials.** Provider API keys (`ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, …) are split out of the inherited env. SUT commands launch
  with `include_provider_credentials=False`, and any explicit env handed to a
  SUT command (e.g. the test_runner's `os.environ` copy) is first passed
  through `scrub_provider_credentials`. A hostile SUT binary therefore cannot
  read the operator's model keys from its environment.
- **Core env only.** SUT children still receive `PATH`, `HOME`, `LANG`,
  `LC_ALL`, `TZ`, `TMPDIR` so legitimate runners (including Playwright) work.

Model-CLI invocations keep `include_provider_credentials=True` (the default),
because those CLIs need the keys to authenticate.

What remains the operator's responsibility: only point `sut.*` commands at
binaries you trust, since they execute with your account's privileges.

## 4. Response integrity (deferred)

Responses are unsigned and HTTP-only on loopback. Signing or TLS is **out of
scope** while the server is loopback-only (issue #291 notes this as optional).
Revisit only if dashboard exposure beyond loopback is ever introduced.
