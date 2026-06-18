# Analiza gotowosci RC

Status: superseded
Superseded-on: 2026-05-22
Superseded-by: ../README.md (sekcja Status), tracker issue'ów GitHub
Reason: inwentaryzacja gap'ów to point-in-time snapshot z 2026-05-20; większość
wskazanych blokerów (browser regression, skill eval golden, crawler core,
legacy runtime migrate, CLI stubs, candidate review UI bulk, inbox PDF
extraction status, …) już zamknięta na `main`. Traktuj jako kontekst
historyczny — decyzje podejmuj z `README.md` ("Status") i issue trackera.

Data: 2026-05-20
Branch: `task/rc-readiness-analysis`

## Werdykt

Agentic OS **nie jest jeszcze gotowy jako produktowy Release Candidate** dla
pelnej obietnicy:

> Lokalny Agentic OS zarzadzany z dashboardu lub CLI, konfigurowany pod SUT,
> wykrywajacy potrzeby testow web/API, piszacy wykonywalne testy automatyczne,
> uruchamiajacy je, zakladajacy bugi i publikujacy raporty czytelne dla
> czlowieka.

Repo jest mocnym wewnetrznym RC dla szkieletu runtime, backendu dashboardu,
konfiguracji, lifecycle SUT, runnera raportow i komponentow generatora. Brakuje
jednak zintegrowanej sciezki operatorskiej: od realnie skonfigurowanego SUT i
opisu zadania do zatwierdzonych wykonywalnych testow, uruchomienia, bugow i
raportu bez recznej znajomosci wewnetrznych artefaktow.

Rekomendowana etykieta dzisiaj: **implementation preview / internal RC**, nie
zewnetrzny RC.

## Ocena gotowosci

| Obszar | Ocena | Wniosek |
|---|---:|---|
| Sterowanie z CLI | 70% | Glowne komendy istnieja, ale istotne przeplywy sa jeszcze czesciowe albo stubowane. |
| Sterowanie z dashboardu | 65% | Dashboard i API istnieja; zapis jest bramkowany; pelny E2E UX operatora nie jest domkniety. |
| Konfiguracja | 60% | `config/agentic-os.yml` istnieje i da sie go edytowac, ale domyslne ustawienia nie wystarczaja dla realnego SUT, a doctor daje czesciowo mylacy sygnal. |
| Lifecycle SUT | 70% | Start/stop/healthcheck przez Docker Compose istnieje, ale domyslna konfiguracja repo nie dziala, bo brakuje `docker-compose.yml`. |
| Discovery i planowanie testow | 55% | Analiza umie znalezc kandydatow, ale promocja kandydatow do wykonywalnych pozycji planu jest niedomknieta. |
| Generowanie testow | 45% | Generatory umieja tworzyc Playwright specs z jawnego `PlanItem(generate_now)`, ale zwykly smoke flow CLI nie wygenerowal wykonywalnych testow. |
| Uruchamianie testow i raporty | 80% | Runner i skrypty raportowe dzialaja, a known-bug-red pozostaje czerwony. |
| Bug filing i triage | 55% | Prymitywy bugow/raportow istnieja, ale exact-spec failure -> bug/evidence nie jest spiete end-to-end. |
| Integracja modeli/providerow | 60% | Prompty, skille i invocation primitives istnieja; glowna sciezka operatora nadal jest glownie deterministyczna. |
| Operacyjnosc RC | 58% | Nadaje sie do kontrolowanych wewnetrznych demo; nie jest jeszcze niezawodnym RC sterowanym jedna sciezka z dashboardu lub CLI. |

Laczna gotowosc: **58/100 - BLOCK dla zewnetrznego RC**.

## Zebrane dowody

Komendy i obserwacje z lokalnego repo:

| Check | Wynik |
|---|---|
| `git fetch origin main && git pull --ff-only origin main` | Swiezy `main` zostal pobrany przed utworzeniem brancha zadaniowego. |
| `./scripts/agentic-os.sh init` | Sukces; runtime root `agentic-os-runtime`, config path `config/agentic-os.yml`. |
| `./scripts/agentic-os.sh --json doctor --sut --docker --models` | Komenda dziala, ale raportuje `config_exists=false`, mimo ze `config/agentic-os.yml` istnieje. |
| Doctor SUT check | Raportuje `compose_file missing: docker-compose.yml`, wiec domyslna lokalna konfiguracja SUT nie jest uruchamialna. |
| Doctor model check | Wykryl binarki planner/implementer/reviewer, ale nie sprawdza roli triager. |
| `./scripts/agentic-os.sh --json status` | Runtime i SQLite sa uzywalne, ale status faz nie jest wiarygodnym wskaznikiem gotowosci RC. |
| `python -m pytest` | `236 passed`. |
| `./run-tests.sh --self-check-known-bug` | Zwrocil `1` i nadal wygenerowal `reports/last-run.json` oraz `reports/summary.md`; known bug pozostaje czerwony. |
| Smoke dashboardu na porcie `8876` | `/healthz`, `/api/status` i `/api/config` zwrocily poprawny JSON. |
| Smoke CLI: create/analyze/plan/implement-tests | Work item powstal i analiza pokazala kandydatow, ale `TEST-PLAN.json` mial `0` items, a `generated_v2.skipped=true` z powodem `no_items`. |

## Co dziala dzisiaj

Repo ma realny fundament Agentic OS:

- `scripts/agentic-os.sh` jest domyslnym entrypointem CLI.
- Istnieja `init`, `doctor`, `status`, `up --dashboard-only`, komendy task,
  komendy run i komendy recovery.
- Runtime jest lokalny i oparty o SQLite pod `agentic-os-runtime`.
- Serwer dashboardu wystawia status, konfiguracje, akcje taskow, akcje SUT,
  akcje git, widoki agentow, widoki skilli, sugestie i endpointy autonomii.
- Zapis konfiguracji z dashboardu jest zabezpieczony trybem write i
  ograniczeniem do localhost.
- Istnieja pola config v2 dla trybu SUT, URL-i, zrodel OpenAPI/docs,
  credentials, katalogow testow i runnerow per obszar.
- Lifecycle SUT wspiera Docker Compose start/stop/healthcheck oraz no-op
  start/stop dla trybu online.
- Pipeline work item umie tworzyc taski, analizowac spec, pisac artefakty
  analizy, pisac plan testow, tworzyc artefakty patcha, uruchamiac review/final
  gate i wykonywac skonfigurowany runner.
- Istnieja moduly ingestii OpenAPI i dokumentacji.
- Istnieja generatory Playwright API/UI i maja testy jednostkowe.
- Istnieje parsowanie/klasyfikacja wynikow JUnit, Playwright i Cucumber.
- Istnieja skrypty bugow i raportow: `new-bug.sh`, `copy-reports.sh`,
  `extract-last-run.sh`, `build-summary.sh`.
- Runner tworzy raporty nawet przy niezerowym kodzie testow.
- Znane bugi produktowe moga swiadomie pozostac czerwone.
- Prompty provider-neutral i skille provider-specific sa obecne.

## Braki blokujace

### 1. Automatyczne generowanie testow nie jest spiete end-to-end

Najwiekszy blocker RC to luka miedzy kandydatami z analizy a wykonywalnymi
testami.

W smoke flow `task analyze` raportowal kandydatow API/UI, ale `task plan`
stworzyl `TEST-PLAN.json` z zerowa liczba pozycji. Potem `task implement-tests`
utworzyl tylko Markdownowy skeleton patch pod `tests/generated/...spec.md` i
pominel generator v2 z powodem `no_items`.

To oznacza, ze aktualny domyslny flow operatora moze wygladac na udany, mimo ze
nie tworzy uruchamialnych testow web/API.

### 2. Akceptacja kandydatow nie jest gotowym flow dashboardu

Generator oczekuje pozycji planu z `decision=generate_now`, ale normalny flow
nie daje jeszcze jasnej sciezki dashboard/CLI, ktora zamienia kandydatow z
analizy w decyzje generowania wykonywalnych testow.

To jest uzywalne dla maintainerow znajacych wewnetrzne artefakty, ale nie dla
operatora QA oczekujacego prowadzonego Agentic OS.

### 3. Zarzadzanie z dashboardu jest czesciowe

Dashboard dziala jako lokalna warstwa kontroli, ale nie jest jeszcze pelnym
100% management layer:

- `up` wymaga obecnie `--dashboard-only`; sciezka daemon/orchestrator nie jest
  zaimplementowana.
- Endpointy zapisu sa poprawnie bramkowane, ale domyslny dashboard nie jest
  pelnym panelem zarzadzania bez trybu full/write.
- API dashboardu wystawia akcje taskow, ale pelny UX: skonfiguruj, analizuj,
  zatwierdz kandydatow, wygeneruj testy, zastosuj patch, uruchom, ztriaguj i
  zamknij final gate, nie jest jeszcze udowodniony jako jedna sciezka operatora.

### 4. Gotowosc konfiguracji jest slabsza niz obietnica produktu

Repo ma `config/agentic-os.yml`, ale domyslny config nie wystarcza do
uruchomienia lokalnego SUT:

- `sut.compose_file` wskazuje domyslnie `docker-compose.yml`, ktorego w repo nie
  ma.
- istotne pola v2, takie jak OpenAPI/docs/test runner, sa puste dopoki operator
  ich nie wypelni.
- `doctor` raportuje obecnie `config_exists=false`, bo sprawdza legacy lokalizacje
  zamiast kanonicznego configu.
- rola modelowa triager jest czescia architektury, ale probe modeli w doctor jej
  nie sprawdza.

System da sie skonfigurowac, ale sygnal gotowosci nie jest jeszcze wystarczajaco
wiarygodny dla RC.

### 5. Exact-spec failure -> bug filing nie dziala jako jedna sciezka

Repo ma klocki pod bug-aware behavior:

- parsowanie i klasyfikacje wynikow,
- renderowanie markdowna buga,
- skrypt tworzenia buga,
- generowanie evidence/raportow,
- known-bug-red behavior.

Normalny workflow `run-tests` nie udowadnia jeszcze pelnej sciezki:
failure testu -> klasyfikacja product bug vs known bug vs infra/test issue ->
utworzenie albo aktualizacja `bugs/BUG-NNN` -> podpiecie evidence -> ekran
triage w dashboardzie -> decyzja final gate.

Dla obietnicy Agentic OS to jest blocker releasowy.

### 6. Fallback assertions w generatorach sa za slabe dla strict QA

Generatory API/UI moga emitowac testy wykonywalne, ale fallbacki typu "nie 5xx"
albo "URL nie jest error/404/500" sa slabsze niz dokladne asercje biznesowe.

Dla RC takie slabe asercje powinny wymagac akceptacji operatora albo byc
klasyfikowane jako `needs_operator_decision`, a nie cicho zamieniane na test.

### 7. Czesc CLI jest jawnie niekompletna

Inspekcja kodu pokazala celowo niekompletne albo czesciowe mozliwosci:

- `up` orchestratora bez trybu dashboard-only,
- `down` dla niezaimplementowanej sciezki daemona,
- `logs --follow`,
- `install-shim`.

To jest akceptowalne w wewnetrznym preview, ale powinno byc jawnie oznaczone
jako ograniczenie niezgodne z RC.

## Odpowiedz produktowa na pytanie

Czy repo aktualnie spelnia wymaganie?

**Nie w pelni.**

Da sie nim lokalnie sterowac przez CLI/dashboard primitives. Da sie uruchamiac
testy i produkowac czytelne raporty. Istnieja komponenty potrzebne do
generowania testow API/UI i obslugi bug-aware outcomes. Ale repo nie udowadnia
jeszcze pelnego flow klasy RC, w ktorym operator konfiguruje SUT, system wykrywa
potrzeby testowe, operator zatwierdza je, system generuje wykonywalne testy
web/API, uruchamia je, zaklada bugi i pokazuje koncowe raporty z dashboardu albo
CLI bez znajomosci wewnetrznych formatow artefaktow.

## Rekomendowany gate RC

Nie nazywac aktualnego stanu zewnetrznym RC, dopoki wszystkie ponizsze warunki
nie beda spelnione:

- `doctor --sut --docker --models` jest zgodny z prawda i sprawdza kanoniczny
  config oraz wszystkie skonfigurowane role.
- Przykladowy SUT da sie skonfigurowac z dashboardu albo YAML.
- Kandydaci z analizy staja sie reviewowalnymi pozycjami planu.
- Zatwierdzone pozycje planu generuja realne testy Playwright API/UI.
- Wygenerowane patche da sie zreviewowac, zastosowac, uruchomic i przepuscic
  przez final gate z CLI i dashboardu.
- Failed exact-spec tests tworza albo aktualizuja bugi z evidence.
- Raporty sa czytelne dla czlowieka i podlinkowane z taska/dashboardu.
- Jeden smoke test RC udowadnia cala sciezke na fake SUT.
