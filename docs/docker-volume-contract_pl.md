# Kontrakt wolumenów Dockera

Status: active

Jak system plików hosta i kontener Agentic OS wymieniają dane, gdy OS działa
z Dockera (`docker compose up`). OS działa w kontenerze; SUT jest zewnętrzny.
Decyzja architektoniczna:
`ADR-0001`,
kanoniczny układ katalogów runtime, który ten kontrakt mapuje na hosta:
[`docs/runtime-contract_pl.md`](runtime-contract_pl.md).

Ten dokument definiuje **kontrakt** (które katalogi są montowane, w jakim
trybie odczyt/zapis, bind vs wolumen nazwany oraz właściciel). Konkretna
sekcja `volumes:` żyje w `docker-compose.yml` (issue #353). Weryfikacja
end-to-end — spec zadania wrzucony do hostowego `input/` produkuje artefakty
w hostowym `output/` — jest sprawdzana, gdy Compose podłączy te montaże (#353).

## Układ w kontenerze

Obraz (`Dockerfile`, issue #352) ustala układ w kontenerze:

- `WORKDIR /app` — katalog główny repo OS (`paths.repo_root`). Shim CLI
  rozwiązuje katalog repo na podstawie własnej lokalizacji; w kontenerze nie
  ma gita.
- Użytkownik runtime `agentic`, **uid/gid 10001** (nie-root). Wszystko, co
  kontener zapisuje, należy do 10001.
- Artefakty publiczne zapisywane są pod katalogiem repo: `/app/reports`,
  `/app/bugs`, `/app/evidence`.
- Prywatny stan runtime żyje pod `/app/agentic-os-runtime` (`state.db` + WAL
  SQLite, `events/`, `logs/`, `worktree/`, `leases/`, …).

## Montaże

| Host (domyślnie w compose) | Kontener | Tryb | Typ | Niesie |
| --- | --- | --- | --- | --- |
| `./input` | `/app/input` | **ro** | bind | specy zadań (Markdown), OpenAPI, dokumenty wymagań, config SUT — konsumowane **przez referencję ścieżką** (patrz niżej) |
| `./output/reports` | `/app/reports` | **rw** | bind | raporty przebiegów HTML/JSON |
| `./output/bugs` | `/app/bugs` | **rw** | bind | artefakty bugów |
| `./output/evidence` | `/app/evidence` | **rw** | bind | kopie dowodów do przekazania |
| `agentic-os-runtime` | `/app/agentic-os-runtime` | **rw** | **wolumen nazwany** | `state.db` + WAL, eventy, logi, worktree — przeżywa restarty |

Operator umieszcza materiał w hostowym `input/` (montowane read-only, więc OS
nigdy nie zmieni tego, co dostał) i czyta wyniki z hostowego `output/`.

### Konsumpcja input — przez referencję ścieżką, nie przez skan inboxa

`input/` jest **read-only**, więc niesie wejścia, które OS czyta **w miejscu,
po ścieżce**:

- spec zadania wskazany jako `spec_path` work itemu (np.
  `spec_path: input/login.md`, rozwiązywany pod katalogiem repo przez
  `resolve_repo_path`);
- OpenAPI / dokumenty wymagań / config SUT wskazane z
  `config/agentic-os.yml` (`sut.openapi.sources[].value`,
  `sut.docs.sources[].value`, …) jako `input/<plik>`.

To **nie** jest skanowane przez `inbox ingest` / `inbox synthesize`: te
komendy czytają runtime'owe katalogi intake `inbox/` i `pretask/`
(`INTAKE_DIRNAMES`) i **przenoszą** każdy przetworzony plik do `.archive/` /
`.failed/` — mutacja, której montaż read-only nie wykona. Aby użyć UX
drop-and-ingest w kontenerze, zamontuj hostowy katalog intake **read-write**
pod `/app/inbox` (osobno od read-only `input/`); nie kieruj `inbox ingest` na
`input/`.

### Dlaczego wolumen nazwany dla stanu runtime

`agentic-os-runtime/` trzyma bazę SQLite i jej WAL. Na Docker Desktop
(macOS/Windows) montaż *bind* przekracza granicę VirtioFS / gRPC-FUSE, gdzie
blokowanie plików SQLite jest zawodne i może uszkodzić WAL. **Wolumen
nazwany** żyje na natywnym systemie plików maszyny wirtualnej Linux — bez
ryzyka blokad — i nadal przeżywa `docker compose down`/`up`. Montaże bind są
używane tylko dla artefaktów czytelnych dla człowieka (`reports/`, `bugs/`,
`evidence/`) oraz dla read-only `input/`.

## Ograniczenie konfiguracji — ścieżki artefaktów zostają względne

Domyślny blok `paths:` rozwiązuje się względem katalogu repo:

```yaml
paths:
  reports: reports   # -> /app/reports w kontenerze
  bugs: bugs         # -> /app/bugs
  evidence: evidence # -> /app/evidence
```

Część runtime czyta te klucze konfiguracji; inny kod zapisuje te same
artefakty przez zaszyte `repo_root / "reports"` (bramki, synteza zadań). Oba
rozwiązują się do **tego samego** `/app/reports` tylko dopóki klucze ścieżek
pozostają przy względnych domyślnych wartościach. Zamontowany
`config/agentic-os.yml` **nie może** nadpisywać `paths.reports` / `bugs` /
`evidence` ścieżkami bezwzględnymi — to rozdzieliłoby czytelników honorujących
config od zaszytych zapisujących, a pojedynczy montaż bind przestałby
obejmować oba. Zostaw je względne; montaże się pokryją.

## Wygenerowany framework

Kryterium akceptacji wymienia też „wygenerowany framework testowy" jako
wyjście na hoście. Dziś framework (jego `pom.xml`, `run-tests.sh`,
`src/test/…`) materializuje się w katalogu repo (`/app`). Uczynienie go
samodzielnym, uruchamialnym przez człowieka katalogiem to issue #369, a
wyniesienie go (wraz z raportami i dowodami) do `output/` z linkami z
przewodnika uruchomieniowego to #373. Dopóki to nie wyląduje,
**gwarantowanymi** wyjściami tego kontraktu skierowanymi do operatora są
`reports/`, `bugs/` i `evidence/`; ścieżka frameworka jest finalizowana przez
#369/#373.

## Właściciel i uprawnienia

Kontener zapisuje jako uid/gid **10001**. Cele na hoście muszą być zapisywalne
dla tego id, inaczej zapisy padną z `Permission denied`:

- **macOS / Windows (Docker Desktop):** warstwa współdzielenia plików mapuje
  właściciela dla montaży bind, więc hostowe `input/` i `output/` są zwykle
  zapisywalne od ręki. Wolumen nazwany jest własnością wewnątrz maszyny
  wirtualnej Linux i nie wymaga akcji na hoście.
- **Linux:** katalogi hosta montowane przez bind zachowują realne uid/gid.
  Albo `chown -R 10001:10001 output` (i udostępnij `input/` do odczytu dla
  10001) przed `docker compose up`, albo uruchom usługę jako użytkownik hosta
  (`user:` w Compose, #353), aby kontener zapisywał z id operatora.

Stan runtime na wolumenie nazwanym jest inicjalizowany jako własność 10001
przy pierwszym uruchomieniu, więc nie wymaga ręcznego ustawiania właściciela
na żadnej platformie.

## Rusztowanie katalogów hosta

Repozytorium dostarcza puste katalogi `input/` i `output/` — oraz
**podkatalogi bind-source** `output/reports`, `output/bugs`,
`output/evidence` — każdy z `.gitkeep` (a `input/`/`output/` także krótkim
`README.md`); ich runtime'owa zawartość jest ignorowana przez gita.
Dostarczenie podkatalogów oznacza, że istnieją na świeżym checkoucie, więc
udokumentowane `chown -R 10001:10001 output` je obejmuje, a Docker Compose nie
tworzy ich jako root. Są to domyślne wartości compose dla montaży bind
powyżej — operator wypełnia `input/` i zbiera wyniki z `output/`.
