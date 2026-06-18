# Quality Cat Agentic Web Testing

> Autonomiczny QA dla aplikacji webowych i API. Operator wskazuje SUT
> (System Under Test) przez YAML albo dashboard; OS analizuje OpenAPI/docs,
> generuje wykonywalne testy Playwright TS, uruchamia je, klasyfikuje
> failure jako product bug / known bug / infra / flaky / test bug,
> wystawia bugi Markdown z evidence i pokazuje wszystko w dashboardzie.

**Wersja angielska**: [`README.md`](README.md)

---

## Spis treści

1. [Co to jest](#co-to-jest)
2. [Wymagania](#wymagania)
3. [Instalacja](#instalacja)
4. [Pierwsze uruchomienie](#pierwsze-uruchomienie)
5. [Quick start: od dokumentu do tested feature](#quick-start-od-dokumentu-do-tested-feature)
6. [Konfiguracja SUT](#konfiguracja-sut)
7. [Dashboard](#dashboard)
8. [Pełen workflow operatora](#pełen-workflow-operatora)
9. [CLI reference](#cli-reference)
10. [Modele AI (Opus / Sonnet / Codex / Gemini)](#modele-ai)
11. [Bezpieczeństwo i guardrails](#bezpieczeństwo-i-guardrails)
12. [Rozwiązywanie problemów](#rozwiązywanie-problemów)
13. [Struktura repo](#struktura-repo)

---

## Co to jest

Quality Cat Agentic Web Testing to lokalny framework do automatyzacji QA.
W dokumentacji przewijają się dwie nazwy — **Agentic OS** to orkiestrator
shipowany z tego repo (CLI, dashboard, framework skilli), a **QualityCat**
to domena QA / test-execution dla której produkuje wyniki (raporty bugów,
tagi testów, wygenerowane testy Playwright + TypeScript). Pełny podział
patrz glossary w [`AGENTS.md`](AGENTS.md), a decyzja o stacku w
`ADR-0002`.

Operator dostarcza:

- aplikację (SUT) — **zewnętrzną**: osiągalną po URL-ach web/API plus
  opcjonalnym połączeniu z bazą. OS się z nią łączy i nigdy jej nie
  startuje. (Autostart lokalnego SUT-a przez Compose jest legacy, do
  usunięcia w Wave 17 — patrz
  `ADR-0001`.)
- konfigurację: URL bazowy, API base URL, ścieżki OpenAPI / dokumentacji,
  referencje do credentials;
- task spec (Markdown) opisujący co przetestować.

OS sam:

1. Analizuje SUT (OpenAPI, docs, struktura projektu).
2. Tworzy `TEST-PLAN.md` + `TEST-PLAN.json` z plan items (source ref,
   expected assertion, test data, cleanup).
3. Generuje wykonywalne testy Playwright TS (API + UI) jako patch artifact.
4. Operator zatwierdza patch przez review gate.
5. OS uruchamia testy, zbiera evidence (screenshots, traces, JUnit XML).
6. Klasyfikuje failure i wystawia `BUG-NNN-*.md` dla exact-spec product bugs.
7. Final gate sprawdza zgodność wszystkiego z polityką.

**Co OS nigdy nie robi:**

- Nie modyfikuje SUT (z wyjątkiem `sandbox-sut/` w trybie labowym).
- Nie aplikuje patcha bez jawnego `APPROVE` w review gate.
- Nie osłabia asercji bez decyzji operatora w DB.
- Nie pomija `@known-bug` (znany bug nadal czerwony = exit 1).
- Nie loguje sekretów (credentials są referencjami env/file).

---

## Wymagania

| Komponent     | Wersja                | Wymagane | Uwagi                                              |
|---------------|-----------------------|----------|----------------------------------------------------|
| Python        | 3.13                  | ✅       | Standard library + PyYAML                          |
| PyYAML        | ≥ 6.0                 | ✅       | Jedyna runtime dependency                          |
| SQLite        | wbudowane w Python    | ✅       | `state.db` z WAL                                   |
| Docker + Compose | najnowsza         | ⚪       | Tylko jeśli używasz `sut.autostart`                |
| Node.js + Playwright | LTS         | ⚪       | Tylko jeśli chcesz uruchamiać wygenerowane testy   |
| Modele CLI (opus/sonnet/codex/gemini) | — | ⚪ | Tylko jeśli używasz `models.*` invocation          |
| `gh` (GitHub CLI) | — | ⚪       | Tylko jeśli pushujesz przez HTTPS fallback         |

Platforma: macOS / Linux. Powłoka: zsh albo bash.

---

## Instalacja

```bash
# 1. Sklonuj repo
git clone git@github.com:holi87/agentic-web-testing.git agentic-os
cd agentic-os

# 2. Stwórz venv + zainstaluj zależności runtime + dev (pytest)
python3.13 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"

# 3. Sprawdź instalację
.venv/bin/python -m pytest tests/test_runtime_guards.py
```

Jeśli ostatnia komenda zwróci `5 passed` — środowisko gotowe.

### Uruchomienie z Dockera (bez lokalnego Pythona)

Zbuduj obraz OS i uruchom dashboard jedną komendą — SUT pozostaje zewnętrzny
(ADR-0001), więc nic poza tym nie jest startowane:

```bash
docker compose up
```

Otwórz <http://127.0.0.1:8765/> i zatrzymaj przez `docker compose down`. Wrzuć
specy zadań do `input/`, zbierz wyniki z `output/`, a własny config zamontuj,
aby nadpisać domyślny wbudowany. Kontrakty:
[wolumeny](docs/docker-volume-contract_pl.md) ·
[sieć i sekrety](docs/docker-networking-contract_pl.md).

Na **Linuksie** najpierw udostępnij katalogi output do zapisu dla uid
kontenera (Docker Desktop na macOS/Windows robi to automatycznie):

```bash
mkdir -p output/reports output/bugs output/evidence
sudo chown -R 10001:10001 output input
```

Repo zawiera trzy pomocnicze skrypty:

```bash
./scripts/agentic-os.sh   # główny CLI wrapper
./run-tests.sh             # framework self-test (z bug-aware policy)
./scripts/assertion-guard.py  # diff-time asercja guard
```

---

## Pierwsze uruchomienie

```bash
# 1. Inicjalizuj runtime + config
./scripts/agentic-os.sh init

# 2. Sanity-check layoutu runtime (jeszcze bez SUT/Dockera)
./scripts/agentic-os.sh --json doctor

# 3. Odpal dashboard (foreground, Ctrl+C aby zatrzymać)
./scripts/agentic-os.sh up --dashboard-only --foreground

# 4. Otwórz w przeglądarce
open http://127.0.0.1:8765
```

> Pełna brama `doctor --sut --docker --models` celowo failuje na świeżym
> checkoucie: domyślny config wskazuje na `docker-compose.yml`, którego
> w repo nie ma. Tę wersję uruchom **po** skonfigurowaniu SUT — patrz
> sekcja [Konfiguracja SUT](#konfiguracja-sut) niżej.

`init` tworzy:

- `agentic-os-runtime/state.db` (WAL SQLite + eventy + leasy);
- `config/agentic-os.yml` (skopiowane z `.example`);
- skomentowane sekcje STEP2 v2 (kind, base_url, openapi, docs, credentials,
  tests_dir, tests.api/ui.runner) gotowe do odkomentowania.

---

## Quick start: od dokumentu do tested feature

Najszybsza ścieżka od briefu (Markdown, plain text, DOCX, albo PDF z
wyciągalnym tekstem) do candidate test planu — pięć komend:

```bash
# 1. Init + dashboard z włączonymi zapisami (sesja one-shot)
./scripts/agentic-os.sh init
./scripts/agentic-os.sh up --dashboard-only --foreground --full   # zostaw to działające

# 2. Wrzuć dokument do inbox/ (albo pretask/ dla wielodokumentowych paczek)
cp your-task.md inbox/

# 3. Zsyntezuj jeden task spec z wszystkiego pending
./scripts/agentic-os.sh inbox synthesize --title "Online blog regression"

# 4. Otwórz dashboard i zrób review candidate planu
open http://127.0.0.1:8765
```

Co się dzieje:

- `inbox/` to kanoniczny katalog drop. `pretask/` to widoczny alias dla
  większych paczek (kilka notatek, OpenAPI dump, exploratory checklist).
- `inbox synthesize` czyta wszystkie pending docs, wyciąga endpointy /
  strony / known bugs / ograniczenia danych i pisze JEDEN strukturalny
  task spec. `inbox ingest` to alternatywa one-task-per-document.
- Pomyślnie przetworzone pliki lądują w `<intake>/.archive/<stem>-<UTC-ts>.<ext>`;
  nieudane w `<intake>/.failed/` z sidecar `*.error.txt`.
- Intake PDF obsługuje **wyłącznie PDF z wyciągalnym tekstem** — skany są
  kwarantannowane z hintem o braku OCR (issue #143). Dashboard inbox list
  pokazuje badge `extract: OK / LOW / FAILED` per plik, więc widzisz
  problem zanim klikniesz Ingest.

Pełny pipeline + wariant dashboardowy (Upload + Ingest + Create task
from pending na `/tasks/new`):
[`docs/operator-guide_pl.md` § "Ingest dokumentów zadań"](docs/operator-guide_pl.md#ingest-dokumentów-zadań).

---

## Konfiguracja SUT

### Tryb 1: YAML (zalecane na start)

Edytuj `config/agentic-os.yml`. Minimalny config:

```yaml
sut:
  root: .
  compose_file: docker-compose.yml
  compose_project_name: my-app
  autostart: true
  healthcheck:
    command: ["curl", "-fsS", "http://127.0.0.1:3000/health"]
    timeout_seconds: 30
    retries: 10
  test_runner: ./run-tests.sh
  install_shim_allowed: false
```

Pełen config v2 (opcjonalny, ale zalecany dla STEP2 flow):

```yaml
sut:
  root: .
  kind: web_api                    # web | api | web_api
  base_url: http://127.0.0.1:3000
  api_base_url: http://127.0.0.1:3000/api
  ui_url: http://127.0.0.1:3000
  compose_file: docker-compose.yml
  compose_project_name: my-app
  autostart: true
  healthcheck:
    command: ["curl", "-fsS", "http://127.0.0.1:3000/health"]
    timeout_seconds: 30
    retries: 10
  openapi:
    sources:
      - type: file                 # file | url
        value: docs/openapi.yaml
  docs:
    sources:
      - type: file
        value: docs/requirements.md
  credentials:
    ref_type: env                  # env | file | none
    value: TEST_USER_TOKEN         # tylko nazwa zmiennej, nigdy literal
  tests_dir: tests
  tests:
    api:
      runner: playwright-ts        # playwright-ts | pytest-httpx
    ui:
      runner: playwright-ts
  test_runner: ./run-tests.sh
  install_shim_allowed: false
```

**Walidacje:**

- URL musi być `http://` albo `https://`. Inne schemy → `ConfigError`.
- File paths nie mogą zawierać `..` (path traversal).
- `credentials.value` dla `ref_type: env` musi być nazwą zmiennej (alfanum
  + underscore, nie zaczyna się od cyfry).
- `dashboard.host` musi zostać `127.0.0.1` (zmiana wymaga operator decision).
- Unknown keys → błąd (chyba że są w optional whitelist).

### Weryfikacja SUT, Dockera i konfiguracji modeli

Gdy SUT jest już skonfigurowany, uruchom pełną bramę doctora:

```bash
./scripts/agentic-os.sh --json doctor --sut --docker --models
```

Brama jest zielona tylko gdy **wszystkie** warunki są spełnione:

- `sut.compose_file` istnieje na dysku w `mode: local`, albo jest `null`
  przy `mode: online`;
- `--docker` znajduje CLI `docker` w `PATH` i daemon odpowiada;
- `--models` znajduje skonfigurowane CLI planner/implementer/reviewer/triager
  w `PATH` (patrz `doctor_check_models` w
  `scripts/agentic-os/agentic_os/sut_lifecycle.py`).

Każdy brakujący komponent blokuje wynik — napraw zgłoszony brak i uruchom
ponownie.

### Edycje w dashboardzie i write-enable

Dashboard domyślnie startuje w trybie read-only. Gdy write jest wyłączone,
panele edycji, akcje tasków, edycja agentów, testy connectivity i przełączniki
skilli są celowo disabled; write endpointy zwracają `403`.

Najprostszy tryb operatorski na jedną sesję to in-memory override:

```bash
./scripts/agentic-os.sh up --dashboard-only --foreground --full
```

Dodaj `--no-autostart`, jeżeli chcesz tylko edytować konfigurację i nie chcesz,
żeby dashboard próbował startować lokalnego SUT przez Docker Compose:

```bash
./scripts/agentic-os.sh up --dashboard-only --foreground --full --no-autostart
```

`--full` nie zapisuje zmian w YAML. Po zatrzymaniu i starcie bez `--full`
dashboard wraca do read-only, chyba że włączysz ustawienie YAML poniżej.

Trwałe edycje włączysz w `config/agentic-os.yml`:

```yaml
dashboard:
  host: 127.0.0.1
  port: 8765
  enable_write_endpoints: true
```

Po zmianie YAML zrestartuj dashboard:

```bash
./scripts/agentic-os.sh down
./scripts/agentic-os.sh up --dashboard-only --foreground
```

Potem używaj UI, w tym `/agents`, albo wywołuj write endpointy bezpośrednio,
np.:

```bash
curl -X POST http://127.0.0.1:8765/api/config \
  -H 'Content-Type: application/json' \
  -d @new-config.json
```

Odpowiedzi:

- `200 ok=true` — zapisane do dysku, walidacja przeszła.
- `400` — invalid config, host != 127.0.0.1, brak wymaganych pól.
- `403` — `enable_write_endpoints=false`.

Szybki check:

```bash
curl -fsS http://127.0.0.1:8765/api/config | jq '.dashboard'
```

`enable_write_endpoints: true` oznacza, że edycje są aktywne w tej sesji.
Zostaw `dashboard.host: 127.0.0.1`; dashboard jest lokalnym panelem
operatorskim, nie usługą do wystawienia w LAN.

**GET /api/config** zawsze maskuje `credentials.value`:

```json
{
  "sut": {
    "credentials": {"ref_type": "env", "value": "env:TEST_USER_TOKEN"}
  }
}
```

---

## Dashboard

Domyślnie na `http://127.0.0.1:8765`. Sekcje:

| Sekcja             | Co pokazuje                                                              |
|--------------------|--------------------------------------------------------------------------|
| Full Autonomy      | Czasowo ograniczony, autonomiczny run (analyze → plan → implement)       |
| SUT mode           | Przełącznik local (docker compose) ↔ online (gotowy URL); per-endpoint   |
| Active task        | Bieżący task + tile statystyk runtime (queued/running/failed/blockers)   |
| SUT context        | Bento z 4 blokami: Core / URLs / Runners / Sources / Dashboard           |
| Agents             | Pionowe karty per rola z edycją provider/command, reloadem i connectivity test |
| Skills             | Checkboxy per rola dla załadowanego zestawu skilli                       |
| Patch resolution   | Live chip counters + tabela patches z state (waiting/rejected/abandoned/approved) |
| Suggestions        | Heurystyczna lista następnych kroków                                     |
| Active leases      | Kto trzyma jakie leasy SQLite                                            |
| Last run           | Ostatni `run-tests` exit/raporty                                         |
| Recent events      | SSE stream eventów (severity, payload)                                   |

### Full Autonomy

Wybierz budżet czasowy (min 15, max 720 min — poniżej 60 min dashboard
ostrzega, że pełen cykl build + testy + raporty może się nie zmieścić)
i kliknij **Start full autonomy**. Daemon thread przechodzi po wszystkich
pending work-itemach przez analyze → plan → implement-tests aż do
deadline lub kliknięcia **Stop**. W panelu na żywo widać timer +
event log. Niektóre akcje (np. instalacja systemowego Docker, otwarcie
uprzywilejowanego portu) wymagają sudo — wtedy dashboard musi być
zrestartowany jako root, żeby te kroki się udały. OS i tak zapisuje
co zdążył zrobić.

### SUT mode

- **local** — cykl życia `docker-compose up`. `compose_file` wymagany.
- **online** — gotowy URL (bez dockera). Przełączniki per-endpoint
  bramkują generację testów: `web.enabled=false` pomija UI specs,
  `api.enabled=false` pomija API specs.

Save w panelu zapisuje przez `/api/sut/mode` do
`config/agentic-os.yml`.

Strona Task detail (`/tasks/<id>`) dodaje:

- Timeline (Analyze → Plan → Implement → Review gate → Run tests → Final gate)
- Action buttons (disabled gdy `enable_write_endpoints=false`)
- Blocking patches table z przyciskiem **Abandon** (modal z reason input)
- Meta, spec, artefakty, events stream

**Visual polish (Podfaza 10)**: animowana aurora w tle, kolorystyczne chips
(amber waiting, czerwone rejected z pulse, fioletowe abandoned, zielone
approved z glow), bento grid, magnetic hover, light/dark mode.

---

## Pełen workflow operatora

```bash
# 1. Stwórz task z Markdown spec
./scripts/agentic-os.sh task create path/to/spec.md

# 2. Pobierz id z outputu (TASK-YYYYMMDD-HHMMSS-<slug>)
TASK_ID=TASK-20260519-203000-orders-negative

# 3. Analyze — czyta OpenAPI + docs + skanuje SUT
./scripts/agentic-os.sh task analyze "$TASK_ID"

# 4. Plan — tworzy TEST-PLAN.md
./scripts/agentic-os.sh task plan "$TASK_ID"

# 5. Przejrzyj kandydatów, potem wygeneruj wykonywalne pliki patcha
./scripts/agentic-os.sh task candidates "$TASK_ID"
# zatwierdź wybrane kandydaty API/UI przez task approve-candidate ...
./scripts/agentic-os.sh task implement-tests "$TASK_ID"

# 6. Review gate — zatwierdza patch, ale go nie aplikuje
./scripts/agentic-os.sh run review-gate \
  --scope api \
  --diff agentic-os-runtime/patches/$TASK_ID/<patch>.patch \
  --work-item "$TASK_ID"

# 7. Aplikuj zatwierdzony patch
./scripts/agentic-os.sh run review-gate \
  --scope api \
  --diff agentic-os-runtime/patches/$TASK_ID/<patch>.patch \
  --apply-patch agentic-os-runtime/patches/$TASK_ID/<patch>.patch \
  --work-item "$TASK_ID"

# 8. Lokalny SUT (jeśli compose skonfigurowany)
./scripts/agentic-os.sh run sut-start
./scripts/agentic-os.sh run sut-healthcheck

# 9. Run tests
./scripts/agentic-os.sh run run-tests --work-item "$TASK_ID"

# 10. Final gate (potwierdza, że patche zatwierdzone, bug policy spelniona)
./scripts/agentic-os.sh run final-gate

# 10. Sprzątanie
./scripts/agentic-os.sh run sut-stop
```

### Abandon nieaktualnego patcha

```bash
./scripts/agentic-os.sh task abandon-patch "$TASK_ID" \
  --patch agentic-os-runtime/patches/$TASK_ID/<run>/files/<spec>.diff \
  --reason "rejected after operator review; tracked in BUG-007"
```

Patch zostaje w historii, decyzja idzie do `decisions`, final gate skip
tego patcha.

### Recovery po awarii

```bash
./scripts/agentic-os.sh --json run recovery
sqlite3 agentic-os-runtime/state.db "pragma foreign_key_check;"
```

### Legacy `.agentic-os/` runtime

Starsze checkouty trzymały stan runtime'u w ukrytym katalogu
`.agentic-os/`. Kanoniczna lokalizacja to teraz widoczny
`agentic-os-runtime/`. Jeśli `agentic-os doctor` ostrzega, że oba
katalogi istnieją, użyj `migrate-runtime`:

```bash
./scripts/agentic-os.sh migrate-runtime --dry-run   # zobacz plan
./scripts/agentic-os.sh migrate-runtime             # wykonaj

# zweryfikuj, potem usuń archiwum:
ls .agentic-os.legacy-*/
rm -rf .agentic-os.legacy-*
```

Gdy oba runtime'y już mają `state.db`, migrator odmawia (brak
bezpiecznego automatycznego merge'u). Wybierz źródło prawdy, ręcznie
przenieś drugi, ponów. `--force` jest dostępny, ale archiwizuje
istniejący widoczny stan jako `agentic-os-runtime.clobbered-*/`,
więc nadal możesz go odzyskać.

Jeśli operator celowo skonfigurował `runtime.root: .agentic-os` w
`config/agentic-os.yml`, doctor NIE wyświetla ostrzeżenia —
`.agentic-os/` jest wtedy oficjalnym runtime'em, nie legacy.

---

## Referencja CLI

Pełen kontrakt: [`docs/cli-contract.md`](docs/cli-contract.md).

```bash
./scripts/agentic-os.sh init [--force] [--install-shim [--shim-dir DIR]] [--sample-sut]
./scripts/agentic-os.sh doctor [--sut] [--docker] [--models]
./scripts/agentic-os.sh up [--dashboard-only] [--foreground | --daemon] [--host H] [--port P] [--full] [--no-autostart] [--auto-repair] [--autonomy-minutes N]
./scripts/agentic-os.sh down [--timeout SECONDS]
./scripts/agentic-os.sh status
./scripts/agentic-os.sh logs [--follow] [--lines N] [--file PATH]
./scripts/agentic-os.sh crawler <start-url> [--depth N] [--max-pages M] [--browser]
./scripts/agentic-os.sh migrate-runtime [--dry-run] [--force]
./scripts/agentic-os.sh support-bundle
./scripts/agentic-os.sh inbox [list | ingest | synthesize]

./scripts/agentic-os.sh task create <spec.md>
./scripts/agentic-os.sh task list
./scripts/agentic-os.sh task show <task-id>
./scripts/agentic-os.sh task analyze <task-id>
./scripts/agentic-os.sh task plan <task-id>
./scripts/agentic-os.sh task implement-tests <task-id>
./scripts/agentic-os.sh task abandon-patch <task-id> --patch <p> --reason <r>

./scripts/agentic-os.sh run dry-run [--fake-sut]
./scripts/agentic-os.sh run recovery
./scripts/agentic-os.sh run sut-start
./scripts/agentic-os.sh run sut-healthcheck
./scripts/agentic-os.sh run sut-stop
./scripts/agentic-os.sh run run-tests [--work-item <id>]
./scripts/agentic-os.sh run review-gate [--scope ...] [--diff <f>] [--apply-patch <f>]
./scripts/agentic-os.sh run final-gate

./scripts/agentic-os.sh autonomy [start | stop | pause | resume | status | preflight | follow | bootstrap]
./scripts/agentic-os.sh schedule [add | list | remove | enable | disable | run-now]
./scripts/agentic-os.sh verifications [list | show | override]
./scripts/agentic-os.sh budget [show | set | reset]
./scripts/agentic-os.sh reports [list | show | diff]
./scripts/agentic-os.sh notifications [test]
./scripts/agentic-os.sh transcripts <id>
./scripts/agentic-os.sh git ensure
./scripts/agentic-os.sh sessions summary <id>
./scripts/agentic-os.sh project [list | show | register]
./scripts/agentic-os.sh coverage [list | check]

./scripts/agentic-os.sh --json <subcommand>   # JSON output
```

Kody wyjścia:

| Kod | Znaczenie                                                  |
|-----|------------------------------------------------------------|
| 0   | Sukces                                                     |
| 1   | Product failure (np. test failed, known-bug nadal czerwony) |
| 2   | Infra failure (brak Dockera, healthcheck timeout, config error) |
| 64  | Usage error (zły argument CLI)                             |
| 130 | Ctrl-C / SIGINT                                            |

---

## Modele AI

Konfiguracja w `models.{planner,implementer,reviewer,triager}`. Każdy
wpis ma:

- `provider`: `claude | codex | antigravity | script` (per rolę; patrz
  `models.<role>.fallback` — łańcuch failoverów na rate-limit / quota /
  auth)
- `command`: argv list (pierwszy element musi być na `$PATH`)
- `role`: `opus | sonnet | codex | gemini | script`
- `auto_fire` (opcjonalnie, tylko triager): gdy `true`, triager
  uruchamia się automatycznie po każdym test suite.

Role:

- **planner** — projektuje decyzje, pisze `requirements.md`, planuje
  fazy. Domyślnie: Claude Opus.
- **implementer** — pisze kod pod dyrekcją plannera (specs, init,
  package, verify). Domyślnie: Claude Sonnet.
- **reviewer** — bramkuje diffy (poprawność + zgodność z business
  assumption, argv-only, brak osłabiania assertion). Domyślnie: Codex.
- **triager** — ocena **severity** + **priority** bugów, doprecyzowanie
  opisów, cross-check failed runs vs `bugs/`. Domyślnie: Claude
  (haiku); Codex jako drugi; Antigravity (`agy --model
  gemini-3.1-pro-high`) jako fallback po wyczerpaniu limitów.

Prompty są w `config/prompts/{planner,implementer,reviewer,triager,
bug-adjudication}.md` — neutralne dla providera. Zmiana modelu
wymaga tylko `models.<role>.command`.

Wywołanie modelu:

- Wpisuje wiersz do `model_invocations` (id, task_id, run_id, command JSON,
  exit_code, started_at, finished_at).
- Pisze prompt do `agentic-os-runtime/model-inputs/<id>.txt` po `redact_prompt()`
  (maskuje literal bearer/token/api_key/secret/password).
- Pisze stdout modelu do `agentic-os-runtime/model-outputs/<id>.txt`.
- Brak binarki na `$PATH` → `InfraError` (exit 2).
- Reviewer output musi być strict format (`verdict: APPROVE|REJECT` +
  reason + findings + READY), w przeciwnym razie `ValueError`.

### Skills

Per-role opcjonalne fragmenty promptu w `skills/{provider}/`. Schema
nazwy: `qc-{provider}-{role}-{name}.md` (np.
`skills/gemini/qc-gemini-triager-first-check.md`). Runtime filtruje
automatycznie po aktywnym providerze dla każdej roli, więc
`config/skills.yml` może pre-enable wszystkie 3 providery (claude/
codex/gemini) bez spamu warningami.

Toggle przez `/skills` w dashboardzie albo edycję `config/skills.yml`
(`per_role.<role>.enabled`).

**Bez modeli na PATH** wszystkie operacje czysto deterministyczne (parsery,
generatory, gate) działają. Modele potrzebne tylko do non-deterministic
decyzji (planner), niegenerycznych patchy (implementer), strict review
(reviewer) albo triage bugów (triager).

---

## Bezpieczeństwo i guardrails

| Zasada                                                                | Egzekwowane przez                            |
|-----------------------------------------------------------------------|----------------------------------------------|
| Dashboard server binduje tylko `127.0.0.1`                            | `config._check_const("dashboard.host", "127.0.0.1")` |
| Write endpoints disabled domyślnie                                    | `dashboard.enable_write_endpoints=false`     |
| Wszystkie subprocess argv-only                                        | `runtime/subprocess.py` (no shell=True)      |
| Brak literal sekretów w prompt files                                  | `models.redact_prompt()`                     |
| Brak literal credentials w GET /api/config                            | `config.redact_secrets()`                    |
| Path traversal w configu                                              | `_check_safe_relpath`                        |
| URL scheme tylko http/https                                           | `_check_url`                                 |
| Patch nigdy nie apply'uje się bez APPROVE                             | `gates.merge_patch_if_approved`              |
| `@known-bug` nadal czerwony → exit 1                                  | `run-tests.sh --self-check-known-bug`        |
| `dashboard --volumes` wymaga jawnego flagu                            | `sut_lifecycle.build_compose_argv`           |
| Abandon patcha jest audytowalny (decisions row + gate artifact)       | `workflows.abandon_patch`                    |

---

## Rozwiązywanie problemów

Pełna tabela: [`docs/troubleshooting.md`](docs/troubleshooting.md).

Najczęstsze:

| Objaw                                       | Działanie                                              |
|---------------------------------------------|--------------------------------------------------------|
| `ConfigError: invalid config`               | Lista pól w wiadomości. Sprawdź optional v2 keys.     |
| `POST /api/config` → 403                    | Ustaw `dashboard.enable_write_endpoints: true`.       |
| `sut-start` → exit 2 infra_missing_docker   | Brak Dockera. Wyłącz `sut.autostart` albo zainstaluj. |
| `task abandon-patch` → no patch artifact    | Ścieżka nie zgadza się. `task show <id>` listuje art. |
| Plan gate REJECT trivial assertion          | Asercja `response.ok` / `status 2xx`. Doprecyzuj.     |
| Generator → missing source_ref              | Plan item bez `source_refs[]`. Dodaj ref do docs.     |
| Reviewer model → ValueError                 | Output nie spełnia strict format. Sprawdź outputs/.   |

Logi do podejrzenia:

```bash
agentic-os-runtime/logs/orchestrator.log       # decyzje runtime
agentic-os-runtime/logs/dashboard.log          # HTTP server
agentic-os-runtime/logs/subprocess/<run>.log   # konkretne wywołania
agentic-os-runtime/model-inputs/<id>.txt       # prompt po redact
agentic-os-runtime/model-outputs/<id>.txt      # stdout modelu
agentic-os-runtime/evidence/<run-id>/          # screenshots, manifests, traces
```

---

## Struktura repo

```
.
├── scripts/
│   ├── agentic-os.sh              # wrapper CLI
│   ├── agentic-os/agentic_os/     # pakiet Python
│   │   ├── cli/                   # moduły komend CLI i parser entry
│   │   ├── routes/                # routing HTTP dashboardu i logika
│   │   ├── workflows/             # etapy recovery, run-tests, final-gate
│   │   ├── orchestrator.py        # stan SQLite + dzierżawy (leases)
│   │   ├── gates/                 # walidacja bramki review/final & statyczny przegląd
│   │   ├── config/                # schemat konfiguracji YAML + walidacja
│   │   ├── sut_lifecycle.py       # docker compose argv + healthcheck
│   │   ├── openapi.py             # parser .yaml/.json (Podfaza 04)
│   │   ├── docs_ingest.py         # local docs reader (Podfaza 04)
│   │   ├── sut_discovery.py       # node/python/mixed classifier
│   │   ├── plan_v2.py             # TEST-PLAN.json schema + gate
│   │   ├── generators/
│   │   │   ├── api.py             # generator API Playwright TS
│   │   │   └── ui.py              # generator UI Playwright TS
│   │   ├── results.py             # parsery JUnit/Playwright/Cucumber
│   │   └── models/                # wrapper-y i routing planner/implementer/reviewer/triager
│   └── ...
├── config/                        # config + role prompts + skills.yml
│   ├── agentic-os.yml             # aktywny config
│   ├── agentic-os.yml.example     # template (z opt v2 fields)
│   ├── skills.yml                 # per-role skill enable/disable
│   └── prompts/                   # planner.md, implementer.md, reviewer.md, triager.md
├── skills/                        # qc-{provider}-{role}-{name}.md
│   ├── claude/                    # planner + implementer (default)
│   ├── codex/                     # reviewer (default)
│   └── gemini/                    # triager (Antigravity fallback)
├── agentic-os-runtime/                   # runtime artifacts (gitignored)
├── docs/                          # ADR, contracts, guides
├── tests/                         # pytest suite
├── run-tests.sh                   # framework self-test
├── README.md                      # ten plik wersja EN
└── README_pl.md                   # ten plik wersja PL
```

---

---

## Licencja

MIT — zobacz [`LICENSE`](LICENSE). Możesz dowolnie kopiować, forkować,
modyfikować i redystrybuować; musisz zachować notę o prawach autorskich
oraz informację o repozytorium bazowym (Quality Cat, https://quality-blog.eu
— repo https://github.com/holi87/Agentic-QA-v1) we wszystkich kopiach.

## Kontakt

Issues w repozytorium albo bezpośrednio do Quality Cat (quality-blog.eu).
