# START — szybka konfiguracja AgenticOS

Skrócona ściąga: co przestawić w configu, jakimi flagami uruchomić,
gdzie zajrzeć gdy coś nie działa. Pełna dokumentacja — `README.md`.

## TL;DR (kolejność)

```bash
# 1. venv + deps (runtime + dev dla pytest)
python3.13 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"

# 2. runtime init (tworzy agentic-os-runtime/ + config/agentic-os.yml jeśli brak)
./scripts/agentic-os.sh init

# 3. sanity-check runtime (jeszcze bez SUT/Dockera — te probe wymagają skonfigurowanego SUT)
./scripts/agentic-os.sh --json doctor

# 4. dashboard z włączonymi zapisami (sesyjnie)
./scripts/agentic-os.sh up --dashboard-only --foreground --full

# 5. UI
open http://127.0.0.1:8765
```

Po skonfigurowaniu SUT (`config/agentic-os.yml` — patrz "Minimalny config
SUT" niżej) odpal pełną bramę:

```bash
./scripts/agentic-os.sh --json doctor --sut --docker --models
```

Na świeżym checkoucie ta komenda celowo failuje, bo domyślny
`compose_file: docker-compose.yml` nie istnieje w repo — brama przechodzi
gdy `compose_file` istnieje (tryb local) albo jest `null` przy
`mode: online`.

## Od dokumentu do candidate planu (intake path)

Golden onboarding path to intake `inbox/` + `pretask/` — wrzuć dokumenty
(Markdown / text / DOCX / PDF z wyciągalnym tekstem) na dysk i OS zamieni
je w strukturalny task spec.

```bash
# Wrzuć jeden albo wiele dokumentów…
cp my-feature-brief.md inbox/
cp api-spec.md pretask/                 # pretask/ = widoczny alias dla paczek

# …potem albo zsyntezuj JEDEN task z paczki:
./scripts/agentic-os.sh inbox synthesize --title "Feature X regression"
# …albo zaingestuj jeden task na dokument:
./scripts/agentic-os.sh inbox ingest
```

Ten sam przepływ jest w dashboardzie na `/tasks/new` → **Upload task
document**. Pełna referencja + semantyka extraction-status PDF
(`ok` / `low` / `failed`):
[`docs/operator-guide_pl.md` § "Ingest dokumentów zadań"](docs/operator-guide_pl.md#ingest-dokumentów-zadań)
([EN](docs/operator-guide.md#ingesting-external-task-documents)).

---

## Włączenie zapisów (write endpoints)

Domyślnie dashboard jest **read-only**. Przyciski Edit/Save w UI są wyszarzone,
endpointy POST/PUT/DELETE zwracają `403`.

Dwie metody:

### A) Sesyjnie — flaga `--full` (zalecane do eksperymentów)

```bash
./scripts/agentic-os.sh up --dashboard-only --foreground --full
```

- Override w pamięci, ginie po restarcie procesu.
- Nie zmienia `config/agentic-os.yml`.
- Badge w UI pokazuje **FULL MODE**.
- Włącza też autostart SUT — dodaj `--no-autostart` jeśli chcesz tylko edytować
  config bez podnoszenia dockera:

```bash
./scripts/agentic-os.sh up --dashboard-only --foreground --full --no-autostart
```

### B) Trwale — YAML

Edytuj `config/agentic-os.yml`:

```yaml
dashboard:
  host: 127.0.0.1
  port: 8765
  enable_write_endpoints: true   # ← było false
```

Restart dashboardu:

```bash
./scripts/agentic-os.sh down
./scripts/agentic-os.sh up --dashboard-only --foreground
```

Weryfikacja:

```bash
curl -fsS http://127.0.0.1:8765/api/config | jq '.dashboard'
# powinno zwrócić enable_write_endpoints: true
```

> **Uwaga:** `dashboard.host` musi pozostać `127.0.0.1`. Inny host = `ConfigError`.

---

## Minimalny config SUT

> **Kierunek (Wave 17):** docelowy kontrakt to **zewnętrzny SUT** — OS
> działa w Dockerze i łączy się z SUT-em po URL-ach web/API plus
> opcjonalnym połączeniu z bazą; nigdy go nie startuje. `mode: local` +
> `autostart` (lokalny SUT zarządzany przez Compose poniżej) jest
> **legacy, do usunięcia** — patrz
> `ADR-0001`.
> Poniższy przykład wciąż odzwierciedla obecne zachowanie `main`.

Plik: `config/agentic-os.yml`. Pełna wersja v2 — `README.md` sekcja
"Configuring a SUT". Minimum żeby system ruszył:

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

Per-endpoint flagi (`web.enabled`, `api.enabled`) bramkują generację speców:
`false` → implementer pomija dany typ testów.

---

## Tryb `online` (bez Dockera)

Dla zewnętrznego SUT (gdy `mode: online`), klucze cyklu życia Compose (`compose_file`, `compose_project_name`, `autostart`, `install_shim_allowed`) są opcjonalne i mogą być całkowicie usunięte z pliku konfiguracyjnego (zgodnie z Wave 17/ADR-0001). Wymagane są jedynie: komenda healthcheck, test runner oraz przynajmniej jeden włączony URL web lub API.

Zaczynając od bloku "Minimalny config SUT" powyżej, uprość go do:

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

Healthcheck wciąż jest wymagany — odpytuje on `web.url` / `api.url` (lub inny URL podany w komendzie), aby upewnić się, że zewnętrzny SUT jest osiągalny.

---

## Modele

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

`auto_fire: true` na triagerze = uruchamia się automatycznie w pipeline.
`false` = tylko ręczne wywołanie.

---

## Sprawdzenie czy zapis działa

Gdy badge w UI pokazuje **FULL MODE** ale przycisk Save nadal nieaktywny lub
zwraca 403:

```bash
# 1. czy proces ma override
curl -fsS http://127.0.0.1:8765/api/config | jq '.dashboard'

# 2. czy autonomy session aktywna (też odblokowuje zapisy task-level)
curl -fsS http://127.0.0.1:8765/api/autonomy/status | jq '.active'

# 3. event log
curl -fsS http://127.0.0.1:8765/events?limit=20
```

Jeżeli `/api/config` zwraca `enable_write_endpoints: false` mimo `--full` —
proces dashboardu został zrestartowany bez flagi (np. inny PID). Ubij i odpal
ponownie z `--full`.

---

## Najczęstsze pułapki

- **`port 8765 already in use`** → poprzedni dashboard jeszcze żyje:
  `lsof -i :8765` + `kill <pid>`.
- **`ConfigError: unknown key`** → literówka w YAML lub klucz nie jest na
  whiteliście. Sprawdź `scripts/agentic-os/agentic_os/config/`.
- **`credentials.value`** musi być nazwą env vara (np. `TEST_USER_TOKEN`),
  nie literałem sekretu.
- **Dashboard pokazuje stary config po edycji YAML** → restart procesu.
  Konfig wczytuje się raz przy starcie + na żądanie write endpointów.
- **`--full` nie nadpisuje wartości w UI task** → znany problem: niektóre
  widoki tasków czytają YAML bezpośrednio, ignorując override pamięciowy.
  Dla pełnej spójności użyj metody B (YAML `enable_write_endpoints: true`).
- **Dwa drzewa runtime'u** (`.agentic-os/` i `agentic-os-runtime/`) →
  starszy checkout zostawił legacy runtime. `doctor` ostrzega.
  Skonsoliduj: `./scripts/agentic-os.sh migrate-runtime`
  (`--dry-run` najpierw, żeby zobaczyć plan). Odmawia gdy oba mają
  `state.db` — ręcznie wybierz źródło prawdy. Doctor pomija ostrzeżenie
  jeśli `runtime.root: .agentic-os` jest w configu (świadomy wybór).

---

## Pliki kluczowe

| Plik                                          | Zawiera                                         |
|-----------------------------------------------|-------------------------------------------------|
| `config/agentic-os.yml`                       | Główny config (SUT, modele, dashboard, gates)   |
| `agentic-os-runtime/state.db`                 | SQLite: events, leases, work-items              |
| `scripts/agentic-os.sh`                       | CLI wrapper (up, down, doctor, task, run itp.)  |
| `scripts/agentic-os/agentic_os/config/`       | Definicje schemy i moduł walidacji YAML         |
| `scripts/agentic-os/agentic_os/routes/`       | Routery i logika serwera HTTP dashboardu        |
| `AGENTS.md`                                   | Pełne reguły operacyjne dla agentów             |
| `CLAUDE.md`                                   | Hard rules workflow git dla Claude              |
