# Dashboard operator flow analysis

Status: superseded
Superseded-on: 2026-05-22
Superseded-by: operator-guide.md, dashboard-help.md
Reason: point-in-time analysis from 2026-05-21. All "Implemented changes"
landed on `main` (file-serving allowlist extension, bulk approve, surface-aware
analysis, URL/route parsing, skill exploration depth). Treat as the rationale
record; current behaviour lives in `operator-guide.md` and `dashboard-help.md`.

Date: 2026-05-21.

Scope: dashboard flow for an online public website task, with `sut.web.enabled=true`
and `sut.api.enabled=false`, where the CLI can progress but the dashboard blocks
or misleads the operator.

## Findings

### 1. Artifact links were inaccessible from the dashboard

The dashboard rendered links to operator artifacts under runtime folders such as:

- `.agentic-os/analysis/<task>/requirements.md`
- `.agentic-os/plans/<task>/TEST-PLAN.md`
- `.agentic-os/task-specs/<task>.md`
- `.agentic-os/runs/<run>/triage.md`

`server.py` served `/files/...` through a strict allowlist, but the allowlist
covered only reports, bugs, evidence, patches and subprocess logs. Dashboard
links to analysis, plans, task specs and runs therefore returned 404.

The hidden dot-folder was not the direct cause. The problem was the server-side
allowlist, not filesystem visibility in the browser. Private runtime files such
as `.agentic-os/state.db` still must not be served.

### 2. Candidate review had no bulk approval action

The CLI supports approving candidates one by one, but the dashboard only exposed
per-row Approve / Reject / Needs decision buttons. For exploratory public-site
work this creates unnecessary manual work and makes the dashboard feel incomplete
against the CLI.

Bulk approval must still be conservative: it should approve only runnable API/UI
candidates and skip manual-only buckets such as security, accessibility, already
rejected, already approved, or not-testable candidates.

### 3. Disabled API surface was ignored by analysis/planning heuristics

For an online website configured as UI-only, prose such as `GET /rss` or `GET
/sitemap` was treated as API contract input even when `sut.api.enabled=false`.
The planner could then generate API candidates for a site where the operator
explicitly disabled API testing.

This explains bad candidates such as routes inferred from public web text rather
than from an OpenAPI/source-backed API surface.

### 4. URL parsing could turn domains into fake routes

The old route extraction scanned raw text for slash-like tokens. A URL such as
`https://quality-blog.eu/` could leak a domain fragment into route detection
instead of producing the intended homepage route `/`.

The analyzer should parse real URLs first, strip URLs from later heuristic route
scans, and avoid treating domain text as an application path.

### 5. Skills encouraged shallow exploratory coverage

Several provider skills allowed a too-small interpretation of exploratory public
web testing. For a public website, one or two smoke tests are not enough unless
the task explicitly scopes the work that narrowly.

The skill layer needs explicit instructions to discover routes/links, check
assets, observe console errors, include representative pages, and fail review
when coverage is shallow.

## Runtime root decision

The broken dashboard links were caused by the `/files` allowlist, not by the dot
folder itself. A later compatibility pass moved the default runtime root to the
visible `agentic-os-runtime/` directory anyway because it makes operator checks,
screenshots and file inspection easier. Legacy `.agentic-os/` remains supported
when explicitly configured or when it is the only existing runtime directory.

## Implemented changes

- Extended safe dashboard file serving to operator-facing runtime artifacts:
  analysis, plans, task specs and run triage files.
- Kept private runtime state blocked from `/files`, including runtime `state.db`.
- Added dashboard API and UI control for "Approve all runnable" candidates.
- Made bulk approval skip non-runnable/manual-only candidate types.
- Made analysis respect disabled API/web surfaces.
- Made route extraction parse URLs before slash-route heuristics.
- Stopped UI-only tasks from emitting irrelevant OpenAPI-missing warnings.
- Prevented planning from adding OpenAPI-derived items when API is disabled.
- Updated all Claude/Codex/Gemini QualityCat skills to require deeper public web
  exploration and stricter review of shallow coverage.

## Remaining follow-ups

- Add a browser-driven dashboard regression that creates a task, opens candidate
  review, clicks "Approve all runnable", and verifies the rendered table state.
- Add a route crawler/generator mode for public websites so exploratory breadth
  can come from discovered same-origin links instead of only task prose.
- Improve the candidate table editor so operators can bulk-approve with shared
  assertion defaults and then edit individual rows before generation.
