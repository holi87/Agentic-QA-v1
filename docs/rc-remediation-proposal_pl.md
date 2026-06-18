# Propozycja doprowadzenia do RC

Status: superseded
Superseded-on: 2026-05-22
Superseded-by: ../README.md (sekcja Status), tracker issue'ów GitHub
Reason: roadmap remediacji z 2026-05-20 spięty z RC readiness analysis.
Większość workstreamów (crawler, bulk UI review kandydatów, migracja runtime,
browser harness, skill eval golden) już domknięta na `main`. Traktuj jako
kontekst historyczny.

Data: 2026-05-20
Branch: `task/rc-readiness-analysis`

## Cel

Doprowadzic Agentic OS z implementation preview do realnego Release Candidate
dla tej obietnicy operatorskiej:

- konfiguracja SUT z YAML albo dashboardu,
- wykrywanie kandydatow testow API/UI z task spec, OpenAPI, dokumentacji i
  ksztaltu SUT,
- akceptacja albo odrzucenie kandydatow przez operatora,
- generowanie wykonywalnych automatycznych testow web/API,
- uruchomienie testow przez skonfigurowany runner,
- klasyfikacja failure,
- tworzenie albo aktualizacja bugow z evidence,
- publikacja raportow czytelnych dla czlowieka,
- ten sam flow dostepny z CLI i dashboardu.

## P0: Uwiarygodnic sygnal gotowosci

### Naprawic `doctor`

Wymagane zmiany:

- sprawdzac `config/agentic-os.yml` jako kanoniczna sciezke configu;
- legacy `.qualitycat/agentic-os.yml` zostawic tylko jako jawna informacje
  kompatybilnosci, nie jako glowny sygnal `config_exists`;
- uwzglednic role modelowa `triager` w `--models`;
- raportowac brak Docker compose file jako:
  - `error`, gdy `sut.mode=local`, albo
  - `not_applicable`, gdy `sut.mode=online`;
- raportowac puste pola OpenAPI/docs/test runner jako actionable warnings, a
  nie ukryta nieobecnosc.

Akceptacja:

```bash
./scripts/agentic-os.sh init
./scripts/agentic-os.sh --json doctor --sut --docker --models
```

Oczekiwany wynik: komenda raportuje kanoniczny config jako obecny i pokazuje
czytelny status gotowosci per tryb SUT i role modelowe.

### Naprawic widocznosc faz/statusu

Komenda status nie powinna sugerowac, ze fazy implementacyjne sa tylko
`planned`, gdy kod i dokumentacja juz istnieja. Albo trzeba zmigrowac stan faz,
albo ukryc gotowosc faz z dashboardu operatora do czasu, az bedzie wiarygodna.

## P0: Domknac analysis -> plan -> generator

### Promowac kandydatow do pozycji planu

Dzisiaj `task analyze` potrafi znalezc kandydatow, ale `TEST-PLAN.json` moze
byc pusty. To glowny blocker RC.

Wymagane zmiany:

- zapisywac kandydatow analizy w strukturalnym schemacie;
- konwertowac kandydatow API/UI do rekordow `PlanItem`;
- domyslnie oznaczac niepewnych kandydatow jako `needs_operator_decision`;
- pozwolic na `generate_now` tylko gdy:
  - sa referencje do OpenAPI/docs/spec,
  - asercja jest wystarczajaco konkretna,
  - operacja write/destructive nie jest generowana bez cleanupu albo akceptacji.

Akceptacja:

```bash
./scripts/agentic-os.sh --json task analyze <task-id>
./scripts/agentic-os.sh --json task plan <task-id>
```

Oczekiwany wynik: `TEST-PLAN.json` zawiera konkretne pozycje odpowiadajace
candidate summary. System nie moze cicho tworzyc pustego planu, gdy kandydaci
istnieja.

### Dodac komendy CLI do akceptacji

Dodac jawne komendy dla decyzji o kandydatach:

```bash
./scripts/agentic-os.sh task candidates <task-id>
./scripts/agentic-os.sh task approve-candidate <task-id> <candidate-id>
./scripts/agentic-os.sh task reject-candidate <task-id> <candidate-id> --reason "..."
./scripts/agentic-os.sh task mark-needs-decision <task-id> <candidate-id> --reason "..."
```

Generator powinien konsumowac tylko zaakceptowane pozycje `generate_now`.

### Dodac review kandydatow w dashboardzie

Szczegoly taska w dashboardzie powinny miec tabele kandydatow:

- zrodlo: task spec, OpenAPI, docs, SUT discovery;
- obszar: API/UI/security/accessibility/contract;
- endpoint albo route;
- proponowana asercja;
- poziom ryzyka;
- referencja zrodlowa;
- decyzje: generate now, needs decision, reject;
- pole powodu.

## P0: Generowac wykonywalne testy domyslnie

### Traktowac skeleton-only jako brak wygenerowanych testow

`implement-tests` nie powinien zwracac wyniku wygladajacego na sukces, gdy
emituje tylko Markdownowy skeleton, chyba ze operator jawnie wybral tryb
skeleton.

Wymagane zachowanie:

- jezeli plan nie ma wykonywalnych pozycji: zwrocic `needs_operator_decision`;
- jezeli generator nie emituje wykonywalnych plikow: zwrocic status non-success
  w JSON;
- pokazac powod i nastepna akcje operatora.

### Wzmocnic asercje generatorow

Slabe fallbacki nie powinny byc generowane automatycznie.

Wymagane zachowanie:

- testy API potrzebuja oczekiwanego statusu, asercji schema/body albo jawnej
  akceptacji operatora dla smoke-only assertion;
- testy UI potrzebuja semantycznego targetu, widocznego tekstu, roli/nazwy
  accessibility albo jawnej akceptacji operatora dla navigation-only check;
- destrukcyjne operacje API wymagaja cleanupu, strategii danych testowych albo
  jawnego skipu z powodem.

## P0: Spiac tests -> reports -> bugs

### Dodac workflow triage wynikow

Po `run-tests` Agentic OS powinien parsowac dostepne raporty i tworzyc
strukturalny artefakt triage:

```text
agentic-os-runtime/runs/<run-id>/triage.json
agentic-os-runtime/runs/<run-id>/triage.md
```

Kazdy failure powinien miec:

- klasyfikacje: product_bug, known_bug_red, infra, flaky, test_bug,
  inconclusive;
- scenariusz/test zrodlowy;
- linki do evidence;
- proponowana severity i priority;
- sugerowany tytul/tresc buga;
- stan akcji operatora.

### Automatycznie tworzyc bugi dla exact-spec product failures

Gdy failure jest sklasyfikowany jako exact-spec product bug:

- utworzyc albo zaktualizowac `bugs/BUG-NNN.md`;
- skopiowac albo podlinkowac evidence pod `evidence/`;
- dodac wskazowke tagowania known-bug dla testu;
- zostawic test czerwony do czasu zmiany zachowania produktu albo jawnej decyzji
  polityki.

Akceptacja:

```bash
./scripts/agentic-os.sh run run-tests
./scripts/agentic-os.sh run final-gate
```

Oczekiwany wynik: failures sa sklasyfikowane, raporty czytelne dla czlowieka
istnieja, a bugi produktowe maja pliki buga z evidence.

## P1: Domknac dashboard-managed operator flow

### Dodac prowadzony wizard konfiguracji SUT

Dashboard powinien prowadzic operatora przez:

- lokalny Docker vs online SUT;
- web URL i API base URL;
- healthcheck command albo URL;
- zrodlo OpenAPI;
- zrodlo docs;
- credentials jako referencje do zmiennych srodowiskowych;
- lokalizacje output/reportow testowych;
- komendy runnerow API i UI.

Wizard powinien robic walidacje live i zapisywac tylko poprawny config.

### Dodac jedna strone wykonania taska

Strona taska powinna pokazywac caly lifecycle:

1. Task spec.
2. Gotowosc SUT/config.
3. Artefakty analizy.
4. Review kandydatow.
5. Review wygenerowanego patcha.
6. Apply/abandon patch.
7. Uruchomienie testow.
8. Triage failure.
9. Rekordy bugow.
10. Final gate.

Kazdy krok powinien byc uruchamialny z dashboardu i miec odpowiednik CLI.

## P1: Zaimplementowac albo przemianowac autonomie

Obecna autonomia obejmuje tylko czesc sciezki. Dla RC trzeba:

- albo zaimplementowac orchestrator loop: analyze -> plan -> checkpoint
  akceptacji kandydatow -> implement -> review -> apply -> run -> triage ->
  final gate,
- albo przemianowac i opisac obecna autonomie jako "analysis/generation
  assistant", a nie pelna autonomie Agentic OS.

Komenda `up` nie powinna sugerowac istnienia daemona, jesli daemon nie jest
faktycznie zaimplementowany.

## P1: Dodac test proof dla RC

Dodac end-to-end fake SUT proof uruchamiany w CI/lokalnej walidacji.

Minimalna fixture:

- maly fake SUT z jednym endpointem API i jedna trasa UI;
- jeden oczekiwany pass;
- jeden exact-spec product failure;
- jeden przypadek known-bug-red;
- plik OpenAPI i minimalne docs;
- config Agentic OS wskazujacy na fixture.

Wymagany proof:

```bash
./scripts/agentic-os.sh init
./scripts/agentic-os.sh --json doctor --sut --docker --models
./scripts/agentic-os.sh --json task create --spec tests/fixtures/rc-task.md
./scripts/agentic-os.sh --json task analyze <task-id>
./scripts/agentic-os.sh --json task plan <task-id>
./scripts/agentic-os.sh --json task approve-candidate <task-id> <candidate-id>
./scripts/agentic-os.sh --json task implement-tests <task-id>
./scripts/agentic-os.sh --json task review-gate <task-id>
./scripts/agentic-os.sh run run-tests
./scripts/agentic-os.sh run final-gate
```

Akceptacja:

- powstaja wykonywalne pliki Playwright;
- raporty powstaja nawet przy failure;
- exact-spec product failure tworzy buga;
- known-bug scenario pozostaje czerwony;
- dashboard pokazuje ten sam run i artefakty.

## P2: Polerka operatorska

Rekomendowane usprawnienia:

- pozwolic `task create` przyjmowac absolutne sciezki spec przez skopiowanie ich
  do bezpiecznej lokalizacji runtime albo czytelniej udokumentowac wymaganie
  sciezki repo-relative;
- zaimplementowac `logs --follow`;
- zaimplementowac albo usunac `install-shim` z widocznej pomocy;
- doprecyzowac `down` dla trybu dashboard-only i daemon;
- dodac przyklady pod `examples/`: online API, lokalna appka Docker web i mixed
  API/UI SUT;
- dodac akcje dashboardu "copy support bundle", ktora zbiera redacted config,
  doctor output, artefakty taska, last run, linki bugow i logi.

## Sugerowane milestone'y wdrozenia

| Milestone | Zakres | Szacunek |
|---|---|---:|
| RC-0 Truthful operator surface | Naprawa doctor/config/status i widocznych ograniczen. | 1-2 dni |
| RC-1 Candidate promotion | Persist kandydatow, approve/reject flow, gwarancja niepustego planu. | 2-4 dni |
| RC-2 Executable generation | `implement-tests` tworzy uruchamialne specs albo jawny needs-decision status. | 2-3 dni |
| RC-3 Run to bug/report | Klasyfikacja failure i create/update bug/evidence. | 2-3 dni |
| RC-4 Dashboard E2E | Wizard SUT i strona lifecycle taska. | 3-5 dni |
| RC-5 RC proof | Fake SUT i walidacja na zewnetrznym sample SUT. | 2-3 dni |

## Finalna checklista akceptacji RC

Przed nazwaniem repo RC wymagac:

- `python -m pytest` przechodzi.
- `git diff --check` jest czysty.
- `./scripts/agentic-os.sh --json doctor --sut --docker --models` daje zgodny
  z prawda wynik gotowosci.
- Smoke dashboardu `/healthz`, `/api/status`, `/api/config` przechodzi.
- Jeden flow CLI generuje wykonywalne testy API/UI z zaakceptowanych
  kandydatow.
- Jeden flow dashboardu wykonuje ten sam lifecycle.
- `./run-tests.sh --self-check-known-bug` zwraca `1` i nadal zapisuje raporty.
- Exact-spec fake SUT failure tworzy buga z evidence.
- Final gate blokuje, gdy brakuje raportow, triage bugow albo decyzji o patchu.
