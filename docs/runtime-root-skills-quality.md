# Runtime root and skills quality hardening

Status: active

Date: 2026-05-22.

## Architecture decision

Agentic OS now uses `agentic-os-runtime/` as the default runtime root. The
folder is intentionally visible because operators need to inspect SQLite state,
events, plans, generated task specs, model IO and run triage without fighting
hidden-folder behavior in file pickers, dashboards or screenshots.

Legacy `.agentic-os/` remains supported in two cases:

- the operator explicitly keeps `runtime.root: .agentic-os` in config;
- config is missing and `.agentic-os/` is the only existing runtime directory.

All new init/config paths should use `agentic-os-runtime/`.

## Implemented scope

- Runtime path helpers default to `agentic-os-runtime/`.
- `open_runtime()`, `up`, `status`, `logs`, `doctor`, `inbox list` and dashboard
  serving now use the config-aware runtime root instead of hardcoded
  `.agentic-os/`.
- Config examples and the tracked local config use `runtime.root:
  agentic-os-runtime`.
- `.gitignore`, operator docs, dashboard copy and runtime contracts name the
  visible runtime root.
- `/files/...` remains allowlist-based and still blocks private `state.db`.
- Skill init-project prompts no longer reference non-existent template,
  legacy skill-directory or root-standard assets.
- Planner and implementer skills now require a Candidate Quality Contract:
  exact assertion, target surface, business value, test data, cleanup strategy,
  functional tag, lifecycle tag and source reference.

## Quality standard for future skill work

Provider skills are prompt fragments, not standalone global skills. They must
not invent assets the repo does not ship. When required metadata or scaffold
inputs are missing, they should stop with a machine-readable `needs_input:
<field>` instead of producing shallow or fake output.

Exploratory public-web work must not end at one or two smoke checks unless the
task explicitly narrows the scope. Minimum useful coverage includes route/link
discovery, representative pages, asset checks, console errors, accessibility
basics and at least one business-visible assertion per representative flow.

## Remaining follow-ups

- Build a real route crawler/generator so public-site breadth comes from
  same-origin discovery, not just task prose.
- Add rendered dashboard browser regression once the Browser Node REPL tool is
  available in the session.
- Add a first-class packaged demo SUT/test scaffold if `init-project` should
  create runnable Java/Playwright projects from an empty directory without
  operator-supplied stack input.
