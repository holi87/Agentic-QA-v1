# Kontrakt wykonania dwóch trybów

Status: active

Generowane zestawy działają w **dwóch addytywnych trybach** — nie „albo/albo":

- **Tryb A — wewnątrz kontenera OS.** Agentic OS sam uruchamia zestaw (obraz
  Node + Playwright, issue #389) w ramach work itemu.
- **Tryb B — standalone na hoście.** Człowiek uruchamia samowystarczalny bundel
  npm składany przez OS (issue #369,
  [`standalone.py::assemble_standalone_framework`](../scripts/agentic-os/agentic_os/standalone.py))
  przez `./run-tests.sh` — bez Agentic OS, bez Dockera, bez Java/Maven.

**Kontrakt:** *ten sam* zestaw, *te same* pliki, *ta sama* powierzchnia konfigu
działają w obu trybach. Zmiana trybu zmienia **tylko wartości zmiennych
środowiskowych** — nigdy kodu testów, nigdy edycji pliku konfigu. To próg
akceptacji #370.

## Kontrakt zmiennych env (identyczny w obu trybach)

Zarówno runner in-container, jak i standalone `run-tests.sh` czytają lokalizację
SUT i poświadczenia ze środowiska. Generatory emitują specy, które czytają
dokładnie te nazwy:

| Zmienna env | Konsumowana przez | Uwagi |
| --- | --- | --- |
| `API_BASE_URL` | specy API (`api/*.spec.ts` → `process.env['API_BASE_URL']`, baseURL `request` Playwrighta) | wstrzykiwana przez runtime in-container (issue #92); eksportowana przez człowieka w standalone |
| `UI_BASE_URL` | specy UI (`ui/*.spec.ts` → `page.goto(new URL(path, UI_BASE_URL))`) | tak samo w obu trybach |
| *env poświadczeń* (np. `SUT_API_TOKEN`) | auth API — `Authorization: Bearer ${process.env['…']}` | w źródle jest **nazwa** zmiennej, nigdy wartość sekretu (patrz [`standards/playwright-ts-standards_pl.md`](standards/playwright-ts-standards_pl.md) §8) |
| `SUT_DB_*` (opcjonalna, np. `SUT_DB_PASSWORD`) | typowany klient DB / DSN | `ref_type: env`; nazwane słowem-kluczem, by logi redagowały (patrz kontrakt sieciowy) |

Spec sam się pomija lub jawnie failuje, gdy brakuje wymaganej zmiennej env —
nigdy nie ma fallbacku do wbudowanej wartości.

## Co zmienia się między trybami: tylko wartości env

| Zmienna env | Tryb A — in-container, SUT na hoście | Tryb B — standalone na hoście | Remote / staging (dowolny tryb) |
| --- | --- | --- | --- |
| `API_BASE_URL` | `http://host.docker.internal:3000/api` | `http://localhost:3000/api` | `https://staging.example.com/api` |
| `UI_BASE_URL` | `http://host.docker.internal:3000` | `http://localhost:3000` | `https://staging.example.com` |

Pliki pod testem są bajtowo identyczne w każdej kolumnie; różnią się tylko
eksportowane wartości.

## Caveat sieciowy (przebiegi in-container)

W **Trybie A** SUT na hoście osiągasz przez **`host.docker.internal`**, nie
`localhost` (kontenerowy `localhost` to jego własny namespace). Na macOS /
Windows (Docker Desktop) rozwiązuje się automatycznie; na Linuksie Compose
dodaje `extra_hosts: ["host.docker.internal:host-gateway"]`. Pełne reguły
osiągalności, namespace healthchecku i referencja bazy danych żyją w
[`docker-networking-contract_pl.md`](docker-networking-contract_pl.md) — ten
kontrakt odwołuje się do niego, nie duplikuje.

W **Trybie B** zestaw działa wprost na hoście, więc SUT na hoście to zwykły
`localhost`; SUT zdalny używa swojego publicznego URL w obu trybach.

## Uruchamianie każdego trybu

- **Tryb A** — OS steruje `run-tests.sh` wewnątrz obrazu podczas work itemu;
  URL-e pochodzą z configu `sut.*` przełożonego na `API_BASE_URL` /
  `UI_BASE_URL` (issue #92).
- **Tryb B** — wyeksportuj zmienne env i uruchom złożony bundel:

  ```bash
  export API_BASE_URL=http://localhost:3000/api
  export UI_BASE_URL=http://localhost:3000
  export SUT_API_TOKEN=…          # tylko gdy zestaw wymaga auth
  ./run-tests.sh                  # npm ci → playwright test
  ```

---

_Część Wave 17 EPIC D (standalone execution & przekazanie operatorowi). Kontrakt
env i reguły `host.docker.internal` zreframe'owane na stack Playwright + TS wg
ADR-0002 (nazwy `SUT_WEB_URL`/`SUT_API_URL` z issue poprzedzają runtime'owe
`UI_BASE_URL`/`API_BASE_URL`, issue #92). Auto-emisja bundla standalone podczas
przebiegu i wyłożenie go przewodnikiem how-to-run to #371–#373._
