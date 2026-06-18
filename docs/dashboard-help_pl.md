# Pomoc dashboardu

Status: active

Zwięzły przewodnik wewnątrzproduktowy dla dashboardu Agentic OS. Ta sama
treść jest dostarczana jako `docs/dashboard-help.md`, więc strona pomocy
nigdy nie odbiega od drzewa docs. Skróty — [pierwsze uruchomienie](#pierwsze-uruchomienie),
[cykl życia zadania](#cykl-zycia-zadania), [inbox](#inbox-szybki-start),
[pełna autonomia](#pelna-autonomia), [troubleshooting](#troubleshooting),
[legenda](#legenda).

## Pierwsze uruchomienie

1. **Wybierz SUT.** Edytuj `config/agentic-os.yml` (tworzone przez `agentic-os
   init` z `config/agentic-os.yml.example`) — ustaw `sut.root`, `sut.kind`
   oraz URL-e / plik compose, które OS ma badać. Uruchom
   `agentic-os doctor --sut --docker --models` przed pierwszą sesją;
   wykrywa brakujące narzędzia zanim zrobi to dashboard. Jeżeli migrujesz
   ze starszej instalacji laboratorium, w której config leżał w
   `.qualitycat/agentic-os.yml`, `init` przeniesie go przy pierwszym uruchomieniu.
2. **Zdecyduj o zapisach.** `dashboard.enable_write_endpoints: false` to
   bezpieczny default; dashboard renderuje wtedy tryb read-only. Są trzy
   ścieżki odblokowania:
   - przełącz flagę YAML na `true` (trwale),
   - restart z `serve --full` (na czas procesu, ustawiane przez CLI),
   - uruchom sesję pełnej autonomii (trwa tyle co sesja; zapisy do configa
     celowo **nie** są tu odblokowywane).
3. **Upewnij się, że `agentic-os-runtime/` jest zapisywalne.** OS trzyma
   tam SQLite WAL, specyfikacje zadań, runy, evidence i patche.
   `agentic-os init` tworzy layout.
4. **Dodaj pierwsze zadanie.** Albo formularz `/tasks/new` (route
   dashboardu), albo [kafelek inbox](#inbox-szybki-start), albo
   `agentic-os task create docs/example-task.md`. Jeśli SUT to publiczny
   URL (bez Dockera), zobacz przewodnik "Online URL SUT" w
   [`docs/operator-guide.md`](./operator-guide.md); zawiera cztery
   klucze `sut.*`, których potrzebujesz, i wyjaśnia jak napisać plik
   specyfikacji zadania.

## Cykl życia zadania

Każde zadanie przechodzi przez akcje na swojej stronie szczegółowej
(`/tasks/<id>`) mniej więcej w tej kolejności:

1. `analyze` — produkuje `sut-map.json`, `requirements.md`, `risk-map.md`,
   `candidate-tests.md/json` pod `agentic-os-runtime/analysis/<task>/`.
2. `plan` — zamienia kandydatów w `TEST-PLAN.md` pod
   `agentic-os-runtime/plans/<task>/`.
3. **Kandydaci** — przejrzyj wygenerowane przypadki (approve / reject /
   needs-decision) zanim powstanie kod wykonawczy. Decyzje są zapisywane;
   odrzucone pozostają widoczne dla audytu.
4. `implement-tests` — zapisuje wykonywalne patche testów pod
   `agentic-os-runtime/patches/<task>/`.
5. `review-gate` — uruchamia politykę reviewera (poprawność diffa +
   założenia biznesowe). Może zatwierdzić patch, ale go nie aplikuje.
6. `apply-patch` — aplikuje zatwierdzony patch do working tree. Wymagane
   przed `run-tests`.
7. `run-tests` — uruchamia SUT, klasyfikuje failure (`product`, `infra`,
   `flaky`, `known-bug`), pisze `triage.md`, otwiera bugi jeśli polityka
   bramki tak mówi.
8. `final-gate` — block-merge dopóki każda wcześniejsza bramka nie zatwierdzi,
   `triage.md` nie istnieje i scenariusze `known-bug` są dalej czerwone.

Jeśli któryś przycisk jest wyszarzony, zobacz [podpowiedź o zapisach](#pierwsze-uruchomienie).

## Inbox szybki start

Wrzuć dowolne dokumenty `.md`, `.markdown`, `.txt`, `.docx`, `.pdf` do
`./inbox/` lub `./pretask/` (albo skorzystaj z kafelka **Upload task document**
na `/tasks/new`). Następnie:

- naciśnij **Ingest pending** na tym samym kafelku, albo
- naciśnij **Create task from pending**, aby zsyntetyzować jedno zadanie
  z całego zestawu, albo
- uruchom `agentic-os inbox ingest`,
- uruchom `agentic-os inbox synthesize`.

`ingest` parsuje każdy dokument do osobnej specyfikacji zadania pod
`agentic-os-runtime/task-specs/TASK-…md`. `synthesize` tworzy jedną
połączoną specyfikację z odniesieniami do źródeł, wyciągniętymi
wymaganiami, endpointami/stronami, podpowiedziami known-bug i
ograniczeniami danych testowych. Udane źródła trafiają do
`<intake>/.archive/`; nieudane do `<intake>/.failed/` z plikiem
pomocniczym `*.error.txt`. Parsery `.docx` i `.pdf` są opcjonalne —
zainstaluj `python-docx` i `pypdf`, żeby je włączyć. Specyfikacje
markdown mogą deklarować `Priority: PN` i `SUT root: <path>` inline; ingest
to honoruje.

## Pełna autonomia

`Start full autonomy` na stronie głównej startuje sesję samosterującą:
OS zaciąga pending work-items i przeprowadza każde przez analyze → plan
→ implement → review-gate → run-tests → final-gate bez interakcji.
W trakcie aktywnej sesji:

- przyciski akcji zadań odblokowują się nawet gdy
  `dashboard.enable_write_endpoints=false` (UI odpytuje config co 4 s i
  aktualizuje stan — ostrzeżenie pokazuje, która ścieżka odblokowania jest
  aktywna);
- `POST /api/config` oraz zapisy agentów / skilli **pozostają zablokowane** —
  autonomia z założenia nie jest wystarczającą ścieżką odblokowania.

Minimalny zalecany budżet: 60 minut. Zatrzymaj wcześniej przyciskiem
**Stop** lub `POST /api/autonomy/stop`. Jeśli krok wymaga sudo, najpierw
zrestartuj dashboard z podwyższonymi uprawnieniami.

## Troubleshooting

- **Przyciski pozostają wyszarzone** — przeczytaj ostrzeżenie pod
  przyciskami; wymienia wszystkie ścieżki odblokowania. Najczęstszą
  przyczyną jest brak `Start full autonomy` po zostawieniu
  `enable_write_endpoints=false`.
- **`task list` pokazuje wiersze, których nie da się otworzyć** —
  plik specyfikacji został usunięty poza pasmem. Lista oznacza je pigułką
  `MISSING`; kliknij **Prune missing** na `/tasks` lub uruchom
  `agentic-os task prune-orphans`.
- **`infra_missing_docker` / `infra_missing_compose_file`** — sprawdź
  `docs/troubleshooting.md` dla tabeli symptomów.
- **`triager-first-check` STOP** — `reports/last-run.json` jest
  nieświeży; uruchom `run-tests` ponownie przed triage.

Pełna tabela w [`docs/troubleshooting.md`](./troubleshooting.md).

## Legenda

Pigułki statusu (w kolejności progresji): `queued`, `analyzing`, `planned`,
`implementing`, `reviewing`, `running`, `bug_adjudication`, `blocked`,
`done`, `failed`.

Pigułki priorytetu: `P0` (krytyczny), `P1` (wysoki), `P2` (default),
`P3` (niski).

Semantyka exit code dla skryptów run:

- `0` — zielony run.
- `1` — co najmniej jeden scenariusz padł (product / test bug / scenariusze
  known-bug dalej czerwone).
- `2` — infrastructure failure (brak SUT, brakuje Dockera, itp.).
- `130` — operator anulował.
