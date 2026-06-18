# Agentic OS CLI contract

Status: active

- Contract gate: Accepted for implementation
- Phase: `phase/02-codex-runtime-contract`
- Entrypoint: `scripts/agentic-os.sh`

CLI jest publicznym API operatora. Komendy z ADR 0001 są kanoniczne. Nazwy ze starszego planu (`serve`, `start`, `resume`, `dry-run`) są kompatybilnymi aliasami i muszą pozostać, bo używa ich checklista oraz późniejsze fazy.

## 1. Invocation

```bash
scripts/agentic-os.sh <command> [options]
```

Wymagania shima:

- działa z dowolnego katalogu wewnątrz repo;
- wykrywa root repo przez `git rev-parse --show-toplevel`, a gdy git nie jest dostępny, przez położenie shima;
- ustawia `PYTHONPATH=<repo>/scripts/agentic-os`;
- wykonuje `python3 -m agentic_os <command> [options]`;
- propaguje exit code Pythona;
- nie wykonuje workflow w bashu.

Globalne opcje:

| Opcja | Znaczenie |
|---|---|
| `--config <path>` | Domyślnie `config/agentic-os.yml`; legacy `.qualitycat/agentic-os.yml` jest tylko fallbackiem. |
| `--root <path>` | Root repo/SUT dla operacji, domyślnie aktualne repo. |
| `--json` | Output maszynowy JSON na stdout. |
| `--verbose` | Większy tail logów na stderr. |
| `--no-color` | Brak kolorów w output. |

## 2. Exit codes

| Kod | Znaczenie |
|---:|---|
| `0` | Komenda wykonana poprawnie. Dla testów oznacza green. |
| `1` | Product/test fail, w tym czerwony `@known-bug`. |
| `2` | Infra/runtime/config fail. |
| `64` | Błąd użycia CLI: nieznana komenda, brak argumentu, niepoprawna opcja. |
| `130` | Przerwanie przez operatora. |

Komenda CLI nie może zamienić `run-tests.sh` exit `1` na `0`. `status` może zwrócić `0`, mimo że ostatni run miał product fail, bo samo odczytanie statusu się powiodło.

## 3. Canonical commands

### `init`

```bash
scripts/agentic-os.sh init [--force] [--install-shim]
```

Tworzy `agentic-os-runtime/`, inicjalizuje SQLite, tworzy `config/agentic-os.yml`, jeżeli nie istnieje, i waliduje katalogi publicznych artefaktów.

Zasady:

- bez `--force` nie nadpisuje configu;
- `--force` zapisuje backup configu;
- `--install-shim` może zapisać `run-tests.sh` w SUT tylko, jeżeli `sut.install_shim_allowed=true`;
- zapisuje eventy `runtime.initialized`, `config.created`, `db.migration_applied`.

### `doctor`

```bash
scripts/agentic-os.sh doctor [--sut] [--models] [--docker]
```

Sprawdza Python, SQLite, config, prawa zapisu, wolne miejsce, Docker, Compose, JVM/Gradle, port dashboardu oraz lokalne CLI modeli.

Exit:

- `0`, jeżeli wybrane checki przeszły;
- `2`, jeżeli jakikolwiek wymagany check padł;
- `64`, jeżeli opcje są błędne.

### `up`

```bash
scripts/agentic-os.sh up [--foreground] [--dashboard-only] [--stop-existing]
```

Startuje orchestrator i dashboard na `127.0.0.1:8765`. Domyślnie działa w tle. `--foreground` trzyma proces w terminalu. `--dashboard-only` uruchamia tylko FastAPI/HTMX read UI i nie leasinguje tasków.

Zasady:

- przed startem bierze lease `orchestrator`;
- przy konflikcie lease zwraca `2` i pokazuje owner/PID;
- wykonuje recovery scan;
- jeżeli `sut.autostart=true`, wykonuje Docker/SUT lifecycle;
- zapisuje PID w `agentic-os-runtime/pids/`.

### `down`

```bash
scripts/agentic-os.sh down [--stop-sut] [--volumes]
```

Zatrzymuje procesy Agentic OS, flushuje eventy i zwalnia leases.

Zasady:

- `--stop-sut` wykonuje Compose down tylko dla projektu z configu;
- `--volumes` wymaga `--stop-sut` i zapisuje decision operatora;
- brak działającego procesu nie jest błędem.

### `run`

```bash
scripts/agentic-os.sh run <workflow> [--phase <phase-id>] [--tag <expr>] [--dry] [--retry-of <task-id>]
```

Workflowy fazy 03:

| Workflow | Znaczenie |
|---|---|
| `dry-run` | Minimalny task bez realnego SUT; tworzy DB, event, run i manifest. |
| `run-tests` | Uruchamia `sut.test_runner` z zachowaniem exit contract. |
| `recovery` | Wykonuje recovery scan i raportuje akcje. |

Workflowy późniejszych faz mogą dodać `bug-adjudicate`, `qualitycat-sync`, `final-gate`, ale muszą zachować tabele `tasks/runs/events`.

### `task`

```bash
scripts/agentic-os.sh task create <task-spec.md>
scripts/agentic-os.sh task list
scripts/agentic-os.sh task show <task-id>
scripts/agentic-os.sh task analyze <task-id>
scripts/agentic-os.sh task plan <task-id>
scripts/agentic-os.sh task candidates <task-id>
scripts/agentic-os.sh task approve-candidate <task-id> <candidate-id> [--expected-assertion <text>] [--test-data <text>] [--cleanup-strategy <text>] [--target-page <path>]
scripts/agentic-os.sh task reject-candidate <task-id> <candidate-id> --reason <text>
scripts/agentic-os.sh task mark-needs-decision <task-id> <candidate-id> --reason <text>
scripts/agentic-os.sh task implement-tests <task-id>
scripts/agentic-os.sh task abandon-patch <task-id> --patch <rel-path> --reason <text>
```

Operator-level workflow dla zadań testowych widocznych w dashboardzie.

Zasady:

- `create` przyjmuje Markdown z opisem zadania, kopiuje go do
  `agentic-os-runtime/task-specs/` i tworzy rekord `work_items`;
- ID ma format `TASK-YYYYMMDD-HHMMSS-<slug>`;
- nowy task dostaje `status='queued'`, stabilny `spec_path`, `sut_root` i
  `priority`;
- `list` zwraca kolejkę work itemów od najnowszych;
- `show` zwraca task i zarejestrowane artefakty;
- `analyze` zapisuje `sut-map.json`, `requirements.md`, `risk-map.md`,
  `candidate-tests.md` oraz `candidate-tests.json` w
  `agentic-os-runtime/analysis/<task-id>/` i ustawia status `analyzing`;
- `plan` produkuje `TEST-PLAN.md` oraz `TEST-PLAN.json` w
  `agentic-os-runtime/plans/<task-id>/` i ustawia status `planned` (wymaga
  wcześniejszego `analyze`);
- `candidates` listuje strukturalne pozycje `TEST-PLAN.json` oraz liczniki
  decyzji;
- `approve-candidate` promuje jedną pozycję do `generate_now`. Odmawia
  akceptacji, gdy `validate_plan()` znajdzie blokery, np. brak statusu HTTP,
  brak targetu asercji UI albo brak cleanupu dla mutującej metody API;
- `reject-candidate` ustawia `decision='not_testable'` i zapisuje powód w
  notatkach planu;
- `mark-needs-decision` cofa pozycję do review operatora;
- `abandon-patch` zapisuje artefakt z `verdict: ABANDONED`, wstawia wiersz w
  `decisions` (operator, topic `patch_abandoned:<path>`), zachowuje historię
  patcha i odblokowuje final gate. Wymaga `--patch` (ścieżka względna w repo)
  i `--reason` (niepusty tekst). `find_patch_gate_violations` traktuje
  patch z artefaktem `APPROVE` albo `ABANDONED` jako rozstrzygnięty;
- `implement-tests` zawsze generuje reviewowalny skeleton patch w
  `agentic-os-runtime/patches/<task-id>/<hash>.patch` i rejestruje
  `work_item_artifacts.kind='patch'`. Gdy nie ma kandydata zatwierdzonego do
  wykonywalnego generowania, ustawia status `blocked` i zwraca
  `needs_operator_decision`. Gdy co najmniej jeden kandydat jest zatwierdzony,
  emituje też pliki Playwright TS w v2 patch bundle i ustawia status
  `implementing`. Patch nie jest aplikowany; aplikacja idzie wyłącznie przez
  `run review-gate --apply-patch ... --scope api|ui|assertion` przy verdict
  APPROVE;
- runtime nie zapisuje do SUT podczas tworzenia taska.

### `inbox`

```bash
scripts/agentic-os.sh inbox list
scripts/agentic-os.sh inbox ingest
scripts/agentic-os.sh inbox synthesize [--title <task-title>]
```

Intake dokumentów taskowych widoczny w CLI i dashboardzie.

Zasady:

- `list` zwraca pending files z `./inbox/` i `./pretask/`, z pominięciem
  plików ukrytych oraz `.archive/` i `.failed/`;
- `ingest` parsuje każdy pending plik `.md`, `.markdown`, `.txt`, `.docx` lub
  `.pdf` do osobnego task-spec w `agentic-os-runtime/task-specs/`;
- `synthesize` parsuje wszystkie pending files do jednego połączonego task-spec
  ze źródłami, wyciągniętymi wymaganiami, endpointami/stronami, sygnałami
  znanych bugów, ograniczeniami danych testowych i pytaniami otwartymi;
- pomyślne źródła są przenoszone do
  `<intake>/.archive/<stem>-<UTC-ts>.<ext>`;
- nieudane źródła są przenoszone do `<intake>/.failed/` z sidecar
  `<name>.error.txt`;
- `.docx` wymaga `python-docx`; `.pdf` wymaga `pypdf`; błąd parsera jednego
  pliku nie może przerwać reszty batcha.

### `status`

```bash
scripts/agentic-os.sh status [--watch] [--json] [--phase <phase-id>]
```

Pokazuje:

- aktywne leases;
- fazy i ich statusy;
- liczbę tasków według statusu;
- ostatnie runy z exit code;
- otwarte blockery;
- liczbę bugów według severity/status;
- lokalizację ostatniego manifestu.

`--json` zwraca obiekt:

```json
{
  "runtime": "ready|degraded|blocked",
  "db": "ok|missing|corrupt",
  "leases": [],
  "phases": [],
  "tasks": { "queued": 0, "running": 0, "failed": 0 },
  "bugs": { "open": 0, "known": 0 },
  "last_run": null
}
```

### `logs`

```bash
scripts/agentic-os.sh logs [--run <run-id>] [--phase <phase-id>] [--follow] [--lines <n>]
```

Tailuje `agentic-os-runtime/events/current` albo log subprocessu danego runa. `--follow` zachowuje się jak `tail -f`. Brak logu dla istniejącego runa jest infra fail `2`.

## 4. Compatibility aliases

Alias musi być widoczny w `--help` jako alias, nie jako osobna semantyka.

| Alias | Kanoniczne mapowanie | Powód |
|---|---|---|
| `serve` | `up --foreground --dashboard-only` | Checklista i faza 05 oczekują lokalnego dashboardu przez `serve`. |
| `start` | `up` | Plan źródłowy używał `start` dla runtime'u. |
| `resume` | `up --foreground` po wymuszonym `run recovery` | Recovery po przerwanym procesie z planu źródłowego. |
| `dry-run` | `run dry-run` | Fazy 03/04/05/08 oraz finalny fake SUT proof używają tej nazwy bez `run`. |

`resume` wykonuje:

1. walidację configu;
2. recovery scan;
3. start orchestratora w foreground;
4. wznowienie tylko tasków z `payload.resume_allowed=true` albo tasków `queued`.

`dry-run --fake-sut` jest dozwolone dopiero w finalnej fazie fake SUT proof
(faza 15). Wcześniej opcja może zwrócić `64` z komunikatem
`fake SUT is not implemented before final fake SUT proof`.

## 5. Output contract

Human output:

- pierwsza linia mówi, jaka komenda i config są używane;
- błędy idą na stderr;
- sukces `run` pokazuje run id i manifest path;
- product fail `1` pokazuje, że raporty zostały wygenerowane albo dlaczego brakuje evidence;
- infra fail `2` pokazuje klasę błędu i najbliższy log.

JSON output dla `run`:

```json
{
  "ok": false,
  "exit_code": 1,
  "failure_kind": "product",
  "task_id": "01H...",
  "run_id": "run-01H...",
  "manifest_path": "agentic-os-runtime/evidence/run-01H.../manifest.json",
  "reports_path": "reports",
  "bugs_opened": []
}
```

JSON output dla infra fail musi mieć `failure_kind='infra'` i `exit_code=2`.

## 6. Error handling requirements

Nieznana komenda:

```text
error: unknown command '<name>'
try: scripts/agentic-os.sh --help
```

Config invalid:

```text
error: invalid config .qualitycat/agentic-os.yml
path: gates.known_bugs_fail_exit
expected: true
actual: false
```

Lease conflict:

```text
error: orchestrator lease is held
owner: orchestrator
pid: 12345
acquired_at: 2026-05-16T19:02:00Z
hint: scripts/agentic-os.sh status
```

Known bug red:

```text
run-tests failed with product failures (exit 1)
known bugs remain red by policy; this is not an infra failure
reports: reports/
manifest: agentic-os-runtime/evidence/<run-id>/manifest.json
```

## 7. Help text requirements

`scripts/agentic-os.sh --help` musi listować kanoniczne komendy i aliasy:

```text
Commands:
  init
  doctor
  up
  down
  run
  task
  status
  logs

Compatibility aliases:
  serve    -> up --foreground --dashboard-only
  start    -> up
  resume   -> run recovery; up --foreground
  dry-run  -> run dry-run
```

Help nie może sugerować, że `@known-bug` jest greenowany albo wykluczany domyślnie.

## 8. Validation commands for implementers

Po implementacji fazy 03 minimalny zestaw:

```bash
bash -n scripts/agentic-os.sh
python -m py_compile scripts/agentic-os/agentic_os/*.py
scripts/agentic-os.sh init
scripts/agentic-os.sh dry-run
scripts/agentic-os.sh status --json
scripts/agentic-os.sh logs --lines 20
```

Faza 05 dodatkowo:

```bash
scripts/agentic-os.sh serve
```

i ręczne sprawdzenie `http://127.0.0.1:8765`.

Faza 10 dodatkowo:

```bash
scripts/agentic-os.sh task create path/to/spec.md
scripts/agentic-os.sh task list
scripts/agentic-os.sh task show <task-id>
```

Manualny smoke dashboardu powinien potwierdzić `GET /api/tasks`,
`POST /api/tasks` zablokowany przy `dashboard.enable_write_endpoints=false` oraz
tworzenie taska po ustawieniu `enable_write_endpoints=true`.
