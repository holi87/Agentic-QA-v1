# Operator guide

Status: active

Krótka instrukcja dla operatora QA, który chce uruchomić Agentic OS przeciw
swojej aplikacji bez znajomości wewnętrznej architektury.

> **Nazewnictwo.** Ten guide operuje dwiema warstwami: **Agentic OS** to
> orkiestrator (CLI, dashboard, framework skilli z którym tu pracujesz);
> **QualityCat** to domena QA, dla której orkiestrator produkuje output
> (raporty bugów, rodziny cucumber tagów, generowane testy
> `pl.qualitycat` w Javie). Pełne glossary w [`AGENTS.md`](../AGENTS.md).

## Wymagania

- macOS lub Linux.
- Python 3.13 i `PyYAML` (zainstalowane w `.venv/`).
- Opcjonalnie: Docker + `docker compose` dla scenariuszy lokalnego SUT.
- Opcjonalnie: Node.js + Playwright, jeśli generator ma emitować Playwright
  TS i chcesz uruchamiać je tutaj (`npx playwright test`).

## Pierwsze uruchomienie

```bash
git clone <repo> && cd <repo>
python3 -m venv .venv && source .venv/bin/activate
pip install pyyaml
./scripts/agentic-os.sh init
./scripts/agentic-os.sh up --dashboard-only --foreground
open http://127.0.0.1:8765
```

`init` tworzy `config/agentic-os.yml` z `agentic-os.yml.example`.
Zakomentowane sekcje `sut.kind`, `sut.base_url`, `sut.openapi`, `sut.docs`,
`sut.credentials`, `sut.tests_dir`, `sut.tests.api.runner`,
`sut.tests.ui.runner` to opcjonalne pola v2 — odkomentuj te, których
potrzebujesz.

## Tryby konfiguracji

| Tryb               | Plik / endpoint                    | Wymóg                                   |
|--------------------|------------------------------------|-----------------------------------------|
| YAML               | `config/agentic-os.yml`       | Brak                                    |
| Dashboard (write)  | `POST /api/config`                 | `dashboard.enable_write_endpoints=true` |

POST `/api/config`:
- Zwraca `403`, gdy endpointy zapisu są wyłączone.
- Zwraca `400`, gdy próbujesz ustawić `dashboard.host != 127.0.0.1`.
- Waliduje schemat URL (`http`/`https`), blokuje path traversal w
  `openapi.sources` / `docs.sources` / `tests_dir`.

GET `/api/config` zwraca config z zredagowanymi poświadczeniami — wartość
zmiennej środowiskowej pojawia się jako `env:<NAME>`, nigdy w jawnej
postaci.

## Online URL SUT (bez Dockera)

Dla działającego już serwisu (zdeployowany blog, staging, etc.) wskaż
OS-owi publiczny URL bezpośrednio — `docker compose` niepotrzebny. Wrzuć
to do `config/agentic-os.yml`:

```yaml
sut:
  root: .
  mode: online               # już działa, brak komend lifecycle
  compose_file: null
  compose_project_name: online-sut   # placeholder; nigdy nie wywoływany przy mode=online
  autostart: false
  healthcheck:
    command: ["curl", "-fsS", "-o", "/dev/null", "https://example.com"]
    timeout_seconds: 15
    retries: 5
  test_runner: ./run-tests.sh
  install_shim_allowed: false
  web:
    enabled: true
    url: https://example.com
  api:
    enabled: false           # ustaw true + url, gdy serwis ma publiczne API
  tests_dir: tests
  tests:
    ui:
      runner: playwright-ts
```

> `compose_project_name` jest nadal wymagane przez strict config validator
> nawet przy `mode: online`; wartość to placeholder i nigdy nie jest
> wywoływana.

W `mode: online`:

- `agentic-os run sut-start` i `sut-stop` to no-opy; analyzer
  przechodzi prosto do healthchecka.
- `agentic-os doctor --sut` odpala `healthcheck.command` dokładnie tak,
  jak go ustawiłeś — upewnij się, że zwraca 0 dla podanego URL.
- `agentic-os run sut-healthcheck` to jedyna komenda lifecycle, której
  potrzebujesz przed `run-tests`.

Napisz własny task spec (plik Markdown opisujący co testować).
Utwórz task przez:

```bash
./scripts/agentic-os.sh task create inbox/your-task.md
# lub równoważnie:
cp your-task.md inbox/
./scripts/agentic-os.sh inbox ingest
```

Reszta pipeline'u (`task analyze` → `plan` → `candidates` →
`approve-candidate` → `implement-tests` → `review-gate` → `apply-patch` →
`run-tests` → `final-gate`) jest taka sama jak w lokalnym flow Dockerowym
opisanym niżej.

## Co Agentic OS dostarcza dziś

- `agentic-os doctor --sut --docker --models` odpala realne proby SUT, Dockera i CLI modeli.
- `agentic-os run sut-start | sut-healthcheck | sut-stop` obsługuje Docker Compose SUT lifecycle (pomijane przy `sut.mode: online`).
- Config v2 (`sut.kind`, `sut.base_url`, `sut.openapi`, `sut.docs`, `sut.credentials`, `sut.tests_dir`, `sut.tests.{api,ui}.runner`) ładuje się i waliduje; `POST /api/config` jest gated przez `dashboard.enable_write_endpoints` albo `serve --full`.
- Pipeline analizy (`openapi.py`, `docs_ingest.py`, `sut_discovery.py`) produkuje `sut-map.json`, `requirements.md`, `risk-map.md`, `candidate-tests.{md,json}` per task.
- Planner emituje strukturalny `TEST-PLAN.json` plus markdown; `validate_plan()` egzekwuje review gate przed generacją.
- Generatory API + UI emitują wykonywalne Playwright TS spec'i z zaakceptowanych plan items (status / body assertions, env-only credentials, screenshot + trace przy UI failure).
- Result parser obsługuje JUnit / Playwright / Cucumber i klasyfikuje failures na `product_bug` / `known_bug_red` / `infra` / `flaky` / `test_bug` z auto bug markdown.
- Model invocations są argv-only, prompty redactowane, każde wywołanie ląduje w `model_invocations`.
- Dashboard wystawia cały pipeline (analyze → plan → review candidates → generate → review → apply → run → final-gate), włącznie z sesją full-autonomy i inbox/pretask intake pipeline.
- `task abandon-patch <id> --patch <p> --reason <r>` odblokowuje final gate po review operatora.

W toku / częściowo: pełen polish UI dla tabeli candidate review.

Spakowany fake-SUT proof fixture jest w `examples/fake-sut/`: uruchom
`python examples/fake-sut/run-rc-proof.py`, żeby przejść init → inbox
synthesise → analyse → plan → fake-sut report end-to-end w tymczasowym
workspace (issue #137). Deterministyczną połowę pokrywa
`tests/test_fake_sut_proof.py`; online half (implement-tests + realne
run-tests przeciwko `examples/fake-sut/server.py`) jest opisana w
`examples/fake-sut/README.md`.

## Przepływ operatora

```bash
# 1. Konfiguracja
vim config/agentic-os.yml          # albo POST /api/config gdy write=true

# 2. Doctor checki przed pierwszym runem
./scripts/agentic-os.sh --json doctor --sut --docker --models

# 3. Planowanie i akceptacja generowanych testów
./scripts/agentic-os.sh task create inbox/your-task.md
./scripts/agentic-os.sh task analyze <task-id>
./scripts/agentic-os.sh task plan <task-id>
./scripts/agentic-os.sh task candidates <task-id>
./scripts/agentic-os.sh task approve-candidate <task-id> <candidate-id> \
  --expected-assertion "GET /health must return HTTP 200" \
  --cleanup-strategy "read-only endpoint"
./scripts/agentic-os.sh task implement-tests <task-id>
./scripts/agentic-os.sh run review-gate --scope assertion \
  --diff agentic-os-runtime/patches/<task-id>/<patch>.patch \
  --work-item <task-id>
./scripts/agentic-os.sh run review-gate --scope assertion \
  --diff agentic-os-runtime/patches/<task-id>/<patch>.patch \
  --apply-patch agentic-os-runtime/patches/<task-id>/<patch>.patch \
  --work-item <task-id>

# 4. Lokalny SUT (jeśli masz docker-compose.yml)
./scripts/agentic-os.sh run sut-start
./scripts/agentic-os.sh run sut-healthcheck
./scripts/agentic-os.sh run run-tests --work-item <task-id>
./scripts/agentic-os.sh run sut-stop

# 5. Final gate
./scripts/agentic-os.sh run final-gate --work-item <task-id>
```

`run-tests` zapisuje `agentic-os-runtime/runs/<run-id>/triage.json` oraz
`agentic-os-runtime/runs/<run-id>/triage.md`. Exact-spec product failures są
klasyfikowane i, gdy `gates.exact_spec_failure_opens_bug=true`, Agentic OS
próbuje utworzyć `bugs/BUG-NNN-*.md` z linkami do evidence.

## Ingest dokumentów zadań

Wrzuć dokumenty (`.md`, `.markdown`, `.txt`, `.docx`, `.pdf`) do `./inbox/`
albo stagingowego aliasu `./pretask/` i przerób je na task-spec:

```bash
./scripts/agentic-os.sh inbox list      # podgląd plików pending
./scripts/agentic-os.sh inbox ingest    # jeden task na dokument
./scripts/agentic-os.sh inbox synthesize --title "..."  # jeden task z paczki dokumentów
```

Ten sam pipeline jest w dashboardzie na `/tasks/new` → kafelek **Upload task
document** (Upload + Ingest pending + Create task from pending). `ingest`
tworzy osobny task per dokument. `synthesize` czyta wszystkie pending docs,
wyciąga źródła, wymagania, endpointy/strony, znane bugi i ograniczenia danych
testowych, a potem tworzy jeden połączony task-spec. Pomyślnie przetworzone
pliki lądują w `<intake>/.archive/<stem>-<UTC-ts>.<ext>`; nieudane w
`<intake>/.failed/` z sidecar `*.error.txt` opisującym przyczynę. Parsery
`.docx` i `.pdf` są opcjonalne — zainstaluj `python-docx` / `pypdf`, żeby je
włączyć.

**Intake PDF obsługuje wyłącznie PDF z wyciągalnym tekstem — skanowane PDF
nie są OCR-owane.** Każdy pending PDF jest klasyfikowany w momencie listingu
jako `ok`, `low` lub `failed`, a dashboard pokazuje badge przy pliku.
Skany (oraz PDFy z gęstością tekstu poniżej ~50 znaków/stronę) trafiają do
`<intake>/.failed/` z sidecarem opisującym limit. Wyeksportuj źródło jako
tekstowy PDF (np. `Print → Save as PDF` z oryginalnego edytora) albo wklej
treść do `.md` / `.txt`.

### Intake `Type: public-site` — auto-crawler

Otaguj markdownowy doc intake `Type: public-site` i linią `Start URL:`,
żeby `inbox synthesize` automatycznie uruchomił same-origin crawler przed
utworzeniem work itema:

```markdown
# Public site QA sweep

Priority: P2
SUT root: .
Type: public-site
Start URL: https://staging.example.com/

## Expected behavior
Smoke-crawl publicznej strony, raport broken assets i inwentarza routes.
```

Crawl działa na depth 1 / max-pages 10. Odkryte routes lądują w sekcji
"Relevant endpoints or pages" wyrenderowanego speca; broken assets w
"Known bugs". Pełny JSON report jest zapisywany w
`agentic-os-runtime/inbox/crawls/<work_item_id>/crawl-NN.json`, więc kolejne
etapy analyze/plan czytają strukturę bez re-parsowania markdownu.

SSRF guard jest włączony domyślnie — cele loopback / RFC1918 / link-local
są odrzucane (wpis crawla jako `failed`, ale task wciąż się ingestuje).
Testy przeciw lokalnym fixturom HTTP włączają to przez Python keyword
`allow_private_crawl=True` w `synthesize_inbox_task`.

## Screenshoty dashboardu w CI

Job `dashboard screenshots (issue 145)` zapisuje full-page PNG kluczowych
ekranów operatora (home, lista tasków, new task / inbox, task detail z
candidate review, help / support bundle) na każdy push i PR. PNGi
lądują jako artifact `dashboard-screenshots` (retention 14 dni) i każdy
jest porównywany pixel-diffem przeciw zacommitowanemu Linux baseline'owi
pod `tests/snapshots/dashboard/linux/` (issue #166). Brama failuje, gdy
różni się więcej niż `AGENTIC_OS_SCREENSHOTS_DIFF_THRESHOLD` procent
pikseli (default `1.0`); pliki `*.diff.png` z czerwoną maską diffu
trafiają do artifactu.

Pixel-diff brama jest domyślnie włączona tylko na Linuxie — baselines
wzięte z `ubuntu-latest` i Chromium na macOS/Windows renderuje fonty
inaczej. Force-enable poza Linuxem: `AGENTIC_OS_SCREENSHOTS_GATE=1`
(diffy nie będą zerowe).

Lokalnie:

```bash
pytest -m browser tests/test_dashboard_screenshots.py
# PNGi lądują w build/screenshots/. Override przez AGENTIC_OS_SCREENSHOTS_DIR.
```

Odświeżenie baseline'ów po intencjonalnej zmianie UI:

1. Wprowadź zmianę UI na branchu PR-owym.
2. Ściągnij artifact `dashboard-screenshots` z CI runu tego PR-a.
3. Podmień dotknięte pliki pod `tests/snapshots/dashboard/linux/`.
4. Wepchnij update baseline'a w tym samym PR.

Albo bootstrap in-place z Linux runnera:
`AGENTIC_OS_SCREENSHOTS_BOOTSTRAP=1 pytest -m browser
tests/test_dashboard_screenshots.py`.

## Pakiet diagnostyczny (support bundle)

Zbuduj zredagowany pakiet diagnostyczny przy zgłaszaniu issue albo dzieleniu
się reprodukcją:

```bash
./scripts/agentic-os.sh support-bundle
# → agentic-os-runtime/support-bundles/support-YYYYMMDDTHHMMSSZ.tar.gz
```

Flagi (issue #180):

- `--dest <path>` — zapisuje poza katalogiem runtime.
- `--include <list>` / `--exclude <list>` — wybierz podsystemy z
  `config,doctor,events,runs,bugs`. Wzajemnie wykluczające.
- `--no-redact` — osadza plik config verbatim. Manifest zapisuje wybór
  (`redacted: false`); używaj tylko gdy kontrolujesz miejsce docelowe pakietu.
- `--tag <name>` — sufiks w nazwie pliku, ułatwia zarządzanie wieloma
  pakietami z tej samej sesji triage.

Ten sam przepływ jest w dashboardzie na `/help` → kafelek **Support bundle**
(za bramką `dashboard.enable_write_endpoints`). Dashboard zawsze stosuje
domyślny zestaw podsystemów z włączoną redakcją; flagi są dostępne tylko
przez CLI.

Pakiet zawiera:

- `MANIFEST.json` z rozmiarami plików i flagą truncation;
- `config/agentic-os.yml` zredagowany — klucze listy zakazanej
  (`api_key`, `token`, `password`, `bearer`, `credential`, `client_secret`, …)
  zostają zamienione na `<redacted>`;
- `doctor.json` z `agentic-os doctor --sut --models --docker`;
- `events/*.jsonl` — ogon każdego event-loga;
- `runs/<latest>/...` — manifest + małe artefakty ostatniego runu (limit
  256 KiB/plik; większe są obcięte, manifest pokazuje oryginalny rozmiar);
- `bugs/*.json` i `*.md` z dysku.

**Sprawdź zawartość przed wysłaniem.** Denylista jest konserwatywna, nie
wyczerpująca — artefakty runów, eventy i notki bugowe trafiają tam dosłownie
i mogą zawierać dane wrażliwe operatora.

## Porzucenie zaległego patcha

```bash
./scripts/agentic-os.sh task abandon-patch <task-id> \
  --patch agentic-os-runtime/patches/<task>/<run>/files/x.diff \
  --reason "rejected after operator review; tracked in BUG-007"
```

Patch zostaje w historii, decyzja trafia do `decisions`, final gate
przepuści.

## Wnioski między uruchomieniami (learnings)

Runtime trzyma mały magazyn doradczych wskazówek z historii (niestabilne
scenariusze, jakość dostawców, awarie skilli, luki pokrycia). To tylko
wskazówki — decydują nadal bramki. Planner używa wskazówek `flaky` do
kwarantanny scenariuszy; router dostawców preferuje historycznie lepszych
dostawców per rola.

```bash
./scripts/agentic-os.sh learnings list [--kind flaky]
./scripts/agentic-os.sh learnings show <id>
./scripts/agentic-os.sh learnings forget <id>     # nadpisanie przez operatora
```

Wagi zanikają w czasie (`decay_tau_days = 14`), a wiersze poniżej progu
(`min_weight = 0.05`) są usuwane. Uruchamiaj rozpad co noc przez scheduler,
aby magazyn pozostał świeży:

```bash
./scripts/agentic-os.sh schedule add learnings-decay \
  --cron "0 3 * * *" --action "learnings decay"
```

## Projekty

Runtime adresuje pracę per projekt (#288). Zawsze istnieje jeden projekt
`default` — jego `sut_root` odzwierciedla `sut.root` z konfiguracji — więc
checkout jedno-SUT nie wymaga konfiguracji. Zarejestruj kolejne projekty, aby
izolować ich elementy pracy (a przez #289 — pamięć per projekt):

```bash
./scripts/agentic-os.sh project list
./scripts/agentic-os.sh project register "Quality Cat" --sut-root sites/qc
./scripts/agentic-os.sh project show quality-cat
```

Aktywny projekt rozwiązywany jest wg priorytetu: jawna flaga, potem
`project.active` w `config/agentic-os.yml`, na końcu `default`. Nowe elementy
pracy trafiają do aktywnego projektu; pominięcie bloku `project:` zachowuje
zachowanie jedno-SUT bez konfiguracji.

## Recovery po awarii

```bash
./scripts/agentic-os.sh --json run recovery
sqlite3 agentic-os-runtime/state.db "pragma foreign_key_check;"
```

## Dokumenty referencyjne

- [`docs/architecture.md`](architecture.md) — mapa runtime (moduły, model pracy,
  tabele DB, wiązanie ról modeli, flow gate/learnings/memory). Skompresowane
  podsumowanie w niej jest wstrzykiwane do promptów agentów (`prompt_context`).
- [`docs/security-trust-boundary.md`](security-trust-boundary.md) — auth
  dashboardu, serwowanie `/files/`, granica zaufania podprocesów SUT.
