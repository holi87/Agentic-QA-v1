# Dashboard design tokens

Single source of truth for the visual layer behind the operator dashboard
(epic #316 / Wave 16). Every color, spacing, typography, and surface decision
lives in `scripts/agentic-os/templates/static/dashboard.css` under a
`--token-name` CSS custom property. Templates may **not** introduce new
hard-coded colors, font families, or surface fills — they consume tokens.

The architecture is two-track:

- **Ethereal Glass** — dark mode. Translucent surfaces, soft hairlines,
  subtle radial veil; high-contrast text.
- **Soft Structuralism** — light mode (default). Structured cream-white
  surfaces, sober brand color, calm hairlines.

Both share the same token names. The `:root` block declares the light
defaults; the `@media (prefers-color-scheme: dark)` block flips them.
`color-scheme: light dark` lets native form controls follow the mode.

## Token families

| Family       | Tokens                                                                                       | Notes                                              |
|--------------|----------------------------------------------------------------------------------------------|----------------------------------------------------|
| Surface      | `--bg`, `--bg-veil`, `--surface`, `--surface-strong`, `--surface-inset`                       | Backgrounds + card fills. `--bg-veil` is a triple radial gradient. |
| Border       | `--border`, `--border-strong`, `--ring-outer`, `--ring-inner`, `--hairline-grad`              | Hairlines and ring shadows.                        |
| Text         | `--text`, `--text-soft`, `--text-muted`, `--text-faint`                                       | Four ramps for content, secondary copy, meta, and skeleton text. |
| Brand        | `--primary`, `--primary-strong`, `--primary-soft`, `--focus-ring`                              | Single brand hue plus focus-ring alpha mask.       |
| State        | `--state-ready`, `--state-running`, `--state-degraded`, `--state-blocked`                     | Map autonomy / task status badges to one palette. |

## Typography

Local/offline only (issue #200 — no remote `@import`). Family names kept
in the stack so operators with the fonts installed get the designed
identity; everyone else falls back to the platform stack:

- Sans: `Geist, ui-sans-serif, system-ui, -apple-system, Segoe UI, …`
- Mono: `'Geist Mono', ui-monospace, SFMono-Regular, …`
- Serif: `'Instrument Serif', ui-serif, Georgia, …` (used sparingly on
  hero copy / "no data" placeholders).

## Layout shells

The dashboard has two shell variants:

1. **Standard shell** (most pages) — `<header class="topbar">` carries the
   canonical nav injected via `<!-- DASHBOARD_NAV -->`. The handler in
   `dashboard_server.render_nav` is the single source for nav membership
   so a missing entry shows up in one place, not seven.
2. **Compact detail shell** — `<body class="detail-shell">` on `task.html`
   and `decision.html`. Inherits the topbar/nav but tightens `main`
   padding and uses a `.meta` definition list grid. Defined under
   `.detail-shell { … }` in dashboard.css — see "Wave 16" section.

## Layout utilities (added by #316)

| Class            | Purpose                                                                                 |
|------------------|-----------------------------------------------------------------------------------------|
| `.row-flex`      | `display: flex; gap: 8px; align-items: center` row — displaces inline `style=""` snippets. |
| `.row-flex--wrap`| Modifier for wrap behavior on narrow widths.                                            |
| `.grid-2`        | Two-column grid with even tracks.                                                       |

## Adding new tokens

1. Declare under both `:root` (light) and the `@media (prefers-color-scheme: dark)`
   block so dark mode never falls back to white.
2. Use through `var(--token, fallback)` so a missing token at runtime keeps the page
   legible.
3. Reference, do not redeclare, in template-level `<style>` — those blocks should
   be empty; per-template styles live in dashboard.css under a namespaced class.

## Refreshing screenshot baselines

Visual changes that pass the local suite often shift Linux baselines by
more than the 2% pixel-diff threshold. After landing:

```bash
# wait for CI to run, then
gh run download <run-id> -n dashboard-screenshots -D /tmp/shots
cp /tmp/shots/*.png tests/snapshots/dashboard/linux/
git add -A && git commit -m "test: refresh dashboard baselines for <change>"
```

See `tests/snapshots/dashboard/linux/README.md` for the canonical
provenance rules.

## Anti-patterns

- Hard-coded hex outside dashboard.css — use a token.
- Adding a new `<style>` block in a template — extend dashboard.css under
  a namespaced class instead.
- Setting `style="..."` on layout — use `.row-flex` / `.grid-2` or add a
  new utility class.
- Pulling remote fonts (Google Fonts, etc.) — local stack only (#200).
- Renaming a token without an in-place alias — old class names referenced
  by JS would silently drift.
