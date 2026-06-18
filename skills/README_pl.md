# Skills (PL)

Skille są opcjonalnym, wymiennym wkładem do promptu modelu. Każdy skill
to plik Markdown z YAML frontmatter na początku:

```markdown
---
name: my-skill
description: Krótki opis, kiedy używać.
---

# Skill: my-skill

## When to use
...

## What to do
...
```

## Struktura

```
skills/
├── claude/      # skille dla provider claude (planner/implementer/reviewer/triager)
├── codex/       # skille dla provider codex (planner/implementer/reviewer/triager)
└── gemini/      # skille dla provider gemini / Antigravity
```

Operator może dodać własne skille przez dashboard (`/skills` → Install
z URL) albo ręcznie wrzucić plik do odpowiedniego katalogu.

## Aktywacja

Edytuj `config/skills.yml` (per-role enabled list) albo użyj dashboard
toggle'i. Domyślnie wszystkie skille są **disabled** — operator
świadomie włącza co potrzeba.

## Format frontmatter

| Pole          | Wymagane | Opis                                              |
|---------------|----------|---------------------------------------------------|
| `name`        | ✅       | Unikalna nazwa (musi pasować do filename bez .md) |
| `description` | ✅       | 1–3 zdania kiedy używać                           |
| `tags`        | ⚪       | Lista tagów dla filtrowania                       |
| `min_version` | ⚪       | Min wersja Agentic OS (semver)                    |

## Security

- Skill nie może zawierać literal sekretów (validator odrzuca).
- Path traversal w skill ID blocked.
- Skille z external URL wymagają operator decision per host.

## Wymagania dla skili providerowych (`skills/{provider}/*.md`)

Skille providerowe zakładają konkretny runtime — bez tych elementów
odpalą się, ale efekty będą rozjechane.

### Claude runtime

- **Tryb caveman aktywny** — każdy skill ma blok `## Communication`
  wymagający `Mode: caveman` (drop articles/filler/pleasantries,
  fragments OK; kod/commits/security: normalnie). W Claude Code włącz
  `/caveman lite|full|ultra` przed sesją albo zostaw default `full`
  jeśli operator tak skonfigurował hooki. Bez caveman tokeny się
  marnują, a audyt zwracać będzie nadmiarowe odpowiedzi.
- **Output language: English** — wszystkie artefakty (`requirements.md`,
  `bugs/BUG-NNN-*.md`, `reports/reviews/*.md`, commit messages) po
  angielsku. Polskie wyjątki tylko w bezpośredniej rozmowie operatora
  z asystentem.
- **Subagents OK** — skille pozwalają na równoległe `Agent` calls dla
  niezależnych slice'ów. Wymaga modelu, który ma narzędzie `Agent`
  (Claude Code, Antigravity).

### Codex runtime

- **Prompt injection, nie globalny `SKILL.md`** — pliki
  `skills/codex/*.md` są wstrzykiwane przez Agentic OS jako fragmenty
  promptu. Nie są paczkami `~/.codex/skills/<name>/SKILL.md`, więc nie
  tworzymy tu folderów `agents/openai.yaml`.
- **Brak rekurencyjnego uruchamiania Codex** — skill nie może instruować
  modelu, by odpalał `codex "$(cat skills/codex/...)"`. Runtime już
  podał treść skilla w promptcie.
- **Brak narzędzi tylko dla Claude** — skille Codex nie mogą wymagać
  `AskUserQuestion` ani `Agent`. Gdy brakuje danych, mają przerwać z
  krótkim `needs_input: <field>` i konkretną listą braków.
- **Output language: English** — artefakty, raporty review i commit
  messages pozostają po angielsku, tak jak w pozostałych providerach.

### Kontekst projektu

- **`AGENTS.md` w roocie repo** — twardy kontrakt workflow git
  (branch-per-task, PR-only do main, zakaz `--no-verify`). Provider
  musi mieć go w kontekście; bez tego pliku skille mogą próbować pisać
  bezpośrednio do `main`.
- **`CLAUDE.md` (root + per-katalog)** — dodatkowe reguły specyficzne
  dla projektu. Skille NIE zawierają polityk git/PR — polegają na
  CLAUDE.md.
- **`qualitycat-standards/` w projekcie testowym** — skopiowane z
  `docs/standards/{qa-standards,playwright-ts-standards,bug-reporting,cucumber-tags}.md`.
  Skille cytują je po nazwie (§N), więc brak któregokolwiek = błąd
  "standards reference not found" w środku skilla.
- **`requirements.md`, `MCP_INVENTORY.md`, `STATUS.md`** — produkowane
  przez `planner-analyze-task` jako pierwszy krok i konsumowane przez
  każdy kolejny skill. Bez nich `design-features`, `verify`,
  `final-gate` nie mają punktu odniesienia.

### Narzędzia w sandbox / SUT

- **Node + Playwright toolchain** — `npm ci`, `npx playwright test`,
  `npm run lint` / `npm run typecheck` muszą działać. Skille
  `implementer-verify`, `init-project`, `reviewer-validate-*` wprost je wołają.
- **Raporter Playwright** — `npx playwright test` musi generować raport HTML;
  `verify` i `final-gate` wymagają `playwright-report/`.
- **Skrypty pomocnicze** — `scripts/copy-reports.sh`,
  `scripts/extract-last-run.sh`, `scripts/new-bug.sh`,
  `scripts/build-summary.sh` oraz `run-tests.sh`. Są kopiowane z tego
  repo Agentic OS przez `init-project`; bez nich `verify` i
  `triager-*` nie mają jak pisać do `reports/last-run.json` ani
  `bugs/README.md`.
- **`AGENTIC_OS_HOME` env var** (Agentic OS root) —
  `implementer-init-project` wymaga exportowanego `AGENTIC_OS_HOME`
  wskazującego na root frameworka (katalog z `skills/`, `scripts/`,
  `docs/standards/`, `config/prompts/` i `run-tests.sh`). Python
  runtime pod `scripts/agentic-os/` **nie czyta** tej zmiennej; to
  kontrakt skill-runtime egzekwowany przez providera LLM, gdy
  `init-project` startuje w świeżym katalogu projektu kontestowego.
  STOP, jeżeli zmienna nie jest ustawiona (albo alternatywna flaga
  `--agentic-os-home <path>` jest pominięta).
- **Playwright + MCP / browser** — `planner-explore-sut` i
  `implementer-implement-ui` zakładają dostęp do Playwright (Java
  driver dla testów; MCP / przeglądarka dla manualnej eksploracji).

### Konwencje wymuszane przez skille

**Prefixy commit messages** — każdy skill który robi `git commit`
trzyma się tej tabelki. Operator NIE powinien rozjeżdżać konwencji w
ręcznych commitach na tych samych artefaktach.

| Rola               | Prefix        | Przykład                                            |
|--------------------|---------------|-----------------------------------------------------|
| planner (artefakt) | `feat:`       | `feat: capture requirements and external systems inventory` |
| planner (eksploracja) | `docs:`    | `docs: SUT exploration findings`                    |
| implementer (init/package) | `chore:` | `chore: init project structure`                  |
| implementer (feature) | `feat:`    | `feat: implement <area> API tests`                  |
| implementer (test fix) | `fix:`    | `fix: address test issue in <area>`                 |
| triager (bug edits) | `docs:`      | `docs: refine BUG-NNN reproduction steps + evidence` |
| reviewer           | `docs:`       | `docs: final gate verdict`                          |

**Ścieżki raportów reviewerów** — wszystkie pliki review siedzą pod
`reports/reviews/<role>.md`:

- `reports/reviews/features.md` — wyjście `reviewer-validate-features`
- `reports/reviews/tests.md` — wyjście `reviewer-validate-tests`
- `reports/reviews/security.md` — wyjście `reviewer-validate-security`
- `reports/reviews/final-gate.md` — wyjście `reviewer-final-gate`

**Ordering skili** — kolejność w pętli sesji:

1. `implementer-init-project` (raz, na starcie)
2. `planner-analyze-task` → `planner-explore-sut` → `planner-design-features`
3. `reviewer-validate-features` (gate przed implementacją)
4. `implementer-implement-api` / `implementer-implement-ui` (slice'y)
5. `implementer-verify` (po każdym slice) — klasyfikuje failures
   inline
6. `reviewer-validate-tests` (po slice'ach implementacji)
7. `reviewer-validate-security` (po round 2)
8. `triager-first-check` — post-hoc sweep po finalnym `verify`,
   NIE równolegle do niego
9. `triager-refine-bug` / `triager-severity-priority` (re-triage)
10. `implementer-package` → `reviewer-final-gate` (BLOCKING przed
    submitem)

### Brakujące wymagania = symptomy

| Brak                            | Co zobaczysz                                       |
|---------------------------------|----------------------------------------------------|
| caveman off (Claude)            | Długie odpowiedzi z fillerami; PR-y rosną w hałas. |
| `AGENTIC_OS_HOME` nieustawiony       | `init-project` STOP w kroku 1.                     |
| `AGENTS.md` brak                | Provider może commitować na `main` lokalnie.       |
| `reports/last-run.json` stale   | `triager-first-check` STOP, każe re-run tests.     |
| `qualitycat-standards/` brak    | `validate-tests` / `validate-features` flagują NO-GO. |
| Node / Playwright niedostępne   | `verify` fail na pre-check, brak `playwright-report/`. |
