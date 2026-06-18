# Plan wdrozenia issues z GitHuba

Status: active

Migawka zrodla: otwarte issues GitHub dla `holi87/agentic-web-testing`,
pobrane 2026-05-27 o 10:57 UTC. Otwarte PR-y w momencie pobrania: brak.

## Cel

Ten dokument zamienia aktualne otwarte issues z GitHuba w kolejnosc
wdrozenia. To dokument routingu, nie zamiennik tresci issues. Kazde issue albo
duzy podzakres nadal powinien miec osobny branch taskowy i PR do `main`.

## Aktualne otwarte issues

| Issue | Obszar | Priorytet | Werdykt planistyczny |
|---|---|---:|---|
| #295 `[bug][dashboard] Full autonomy stays idle with online SUT and budget endpoint import deadlock` | dashboard, autonomia, importy budzetu | P1 | Pierwszy release-blocker. Naprawic przed glebszymi funkcjami autonomii. |
| #291 `[security] Hardening round 2 - dashboard auth, file traversal, SUT sandbox` | security, dashboard, granica subprocess | P0 guardrail | Musi wejsc przed rozszerzaniem powierzchni zapisow/autonomii w dashboardzie. |
| #293 `[docs] Architecture context for agents - authored doc + token-efficient injection` | docs, kontekst promptu, budzet tokenow | P1 enabler | Umozliwia wspolny budzet kontekstu dla #287 i #289. |
| #288 `[arch] Project abstraction layer - addressable projects over flat work_items` | storage, config, scope work itemow | P2 enabler | Wymagane przez #289; izolacja przed pamiecia semantyczna. |
| #287 `[autonomy] Learnings producers + prompt injection (#273 follow-up)` | learnings, gate'y, hinty w promptach | P1/P2 enabler | Rozdzielic producentow od prompt injection; injection skoordynowac z #293/#289. |
| #289 `[autonomy] Per-project RAG memory - semantic recall of project history across sessions` | pamiec, wyszukiwanie, kontekst promptu | P2 feature | Zablokowane przez #288; wspoldzieli budzet injection z #293/#287. |
| #290 `[epic][autonomy] True unattended operation - autonomy round 2` | epic autonomii | P1/P2 epic | Wdrazac jako child branche po #295 oraz enablerach pamieci/learnings. |
| #296 `[bug][dashboard] Menu layout jumps between subpages` | dashboard UI | P2 | Niezalezny polish; po blockerach security/online-autonomy, chyba ze blokuje demo. |
| #292 `[epic][refactor] Decompose oversized modules` | cleanup/refactor | P2/P3 epic | Na koncu albo modul po module po przypieciu zachowania testami. |

## Kolejnosc zaleznosci

```text
#295 online SUT autonomy + budget import deadlock
  -> odblokowuje prawdziwe zachowanie full autonomy w dashboardzie

#291 security hardening
  -> musi poprzedzic szersze uzycie zapisow/autonomii z dashboardu

#293 architecture context and shared prompt budget
  -> koordynuje prompt injection dla #287 i #289

#288 project abstraction
  -> wymagane przez #289 per-project memory

#287 learnings producers
  -> zasila #290 proactive skill failover i hinty plannerow

#289 per-project RAG memory
  -> zasila kontekst kolejnej sesji i glebsza autonomie #290

#290 true unattended operation children
  -> konsumuje #287/#289 i domyka pozostale punkty interwencji operatora

#296 dashboard nav stability
  -> niezalezna stabilizacja UI P2

#292 decomposition
  -> behavior-preserving cleanup po pokryciu blockerow
```

## Fale wdrozenia

### Fala 1 - release blocker online autonomy (#295)

Rekomendowany branch: `task/295-online-autonomy-budget`

Zakres:

- Przeniesc `budget_status()` i pomocnicze agregacje SQLite z
  `agentic_os.models.__init__` do lekkiego modulu, np. `agentic_os/budgets.py`.
- Przestawic CLI `budget show` i dashboard `/api/budget/status` na lekki
  import.
- Dodac galaz online-SUT w sciezce pustej kolejki autonomii. Przy
  `sut.mode: online` i `sut.web.enabled: true` discovery ma uzywac zapisanego
  URL-a web, a nie tylko `sut.root`.
- Jezeli pelna synteza taskow ma zostac odlozona do #290, zapisac precyzyjny
  stan blokady zamiast powtarzac wygladajace uzytecznie `idle:awaiting-task`.

Akceptacja:

- Repro z `https://qualitycat.com.pl` nie petli sie bez konca bez actionable
  work albo deterministycznego powodu blokady.
- Rownolegle polling `/api/budget/status` nie importuje calego pakietu
  `agentic_os.models` i nie podnosi `_DeadlockError`.
- Testy pokrywaja zapis konfiguracji online, zachowanie pustej kolejki online i
  lekkie importy budzetu.

Sugerowane checki:

```bash
.venv/bin/python -m pytest \
  tests/test_autonomy_preflight.py \
  tests/test_exploratory_baseline.py \
  tests/test_autonomy_cli_controls.py \
  tests/test_config_v2_dashboard_api.py
./run-tests.sh
```

### Fala 2 - hardening security (#291)

Rekomendowany branch: `task/291-dashboard-security-hardening`

Zakres:

- Dodac guard auth dla unsafe methods w dashboardzie: `POST` / `PUT` /
  `DELETE`. `enable_write_endpoints` nadal okresla dostepnosc funkcji; nowy
  guard potwierdza tozsamosc lokalnego wywolania zapisu.
- Przeaudytowac serwowanie `/files/`, tak aby kazda sciezka resolve'owala sie
  pod dozwolonym rootem, a prywatny runtime state pozostawal zablokowany.
- Zminimalizowac albo jawnie udokumentowac dziedziczone env dla subprocessow
  SUT. Nie oslabiać `require_safe_argv`.

Akceptacja:

- Nieuwerzytelnione unsafe metody dashboardu sa odrzucane; uwierzytelnione
  zapisy nadal dzialaja w trybie lokalnym/full.
- Payloady `/files/` z `../` i absolutnymi sciezkami sa odrzucane testami
  regresji.
- Granica zaufania subprocessow SUT i wykonalne zachowanie minimal-env sa
  udokumentowane.

Sugerowane checki:

```bash
.venv/bin/python -m pytest \
  tests/test_dashboard_origin_guard.py \
  tests/test_dashboard_server.py \
  tests/test_config_v2_dashboard_api.py \
  tests/test_generator_and_subprocess_security.py
./run-tests.sh
```

### Fala 3 - wspolny kontekst architektury (#293)

Rekomendowany branch: `task/293-architecture-context`

Zakres:

- Napisac `docs/architecture.md` i `docs/architecture_pl.md`, zweryfikowane z
  kodem: mapa modulow, tabele runtime DB, model project/work_item/phase/task,
  wiring rol modeli, przeplywy gate/learnings/memory.
- Dodac jedna sciezke skladania prompt context, ktora potrafi wstrzyknac
  skompresowany kontekst architektury i pozniej dzielic budzet z #287 learnings
  oraz #289 memory.
- Zmierzyc i udokumentowac roznice tokenow raw vs compressed context.

Akceptacja:

- Dokumenty EN i PL sa zsynchronizowane w komendach, sciezkach i modelu.
- Prompty agentow niosa ograniczony kontekst architektury bez ukrywania albo
  zastepowania provider-specific skills.
- Testy dowodza, ze injection jest deterministyczny i limitowany budzetem.

Sugerowane checki:

```bash
.venv/bin/python -m pytest \
  tests/test_model_invocation_wrappers.py \
  tests/test_skill_loader_splice.py \
  tests/test_operator_guide_doc_references.py
git diff --check
```

### Fala 4 - addressable projects (#288)

Rekomendowany branch: `task/288-project-abstraction`

Zakres:

- Dodac migracje v14 z tabela `projects` oraz nullable
  `work_items.project_id`, backfillowana do domyslnego projektu z aktualnego
  configu SUT.
- Rozwiazywac aktywny projekt z CLI/config i scope'owac work items, sessions,
  learnings oraz przyszle odczyty memory przez `project_id`.
- Zachowac single-SUT runtime jako sciezke zero-config bez zmiany zachowania.

Akceptacja:

- Istniejaca runtime DB migruje do jednego domyslnego projektu bez zmiany
  zachowania.
- Drugi projekt da sie zarejestrowac, a work itemy pozostaja izolowane.
- #289 ma stabilna granice `project_id` dla wierszy pamieci.

Sugerowane checki:

```bash
.venv/bin/python -m pytest \
  tests/test_runtime_guards.py \
  tests/test_work_item_artifacts.py \
  tests/test_work_item_dependency_link.py \
  tests/test_session_history.py \
  tests/test_learnings.py
./run-tests.sh
```

### Fala 5 - producenci learnings i hinty w promptach (#287)

Rekomendowany branch: `task/287-learnings-producers`

Zakres:

- Dodac detektory-producentow dla `flaky`, `skill_failure` i `coverage_gap`.
- Wszystkie zapisy maja byc advisory i best-effort; blad zapisu learning nie
  moze zepsuc glownego flow.
- Wstrzyknac relewantne learnings do promptow planner/implementer przez wspolny
  budzet kontekstu z #293 i emitowac `learning.consulted`.

Akceptacja:

- Kazdy producent zapisuje learning na realnym zdarzeniu detekcji z testem.
- Prompty planner/implementer niosa ograniczone bloki hintow.
- Istniejace testy store/read/decay nadal przechodza.

Sugerowane checki:

```bash
.venv/bin/python -m pytest \
  tests/test_learnings.py \
  tests/test_results_bug_classification.py \
  tests/test_coverage_architect_autopilot.py \
  tests/test_codex_review_gate_hardening.py \
  tests/test_test_plan_schema_review.py
./run-tests.sh
```

### Fala 6 - per-project RAG memory (#289)

Rekomendowany branch: `task/289-project-rag-memory`

Warunek wstepny: #288 merged. Koncowy budzet injection skoordynowac z #293 i
#287.

Zakres:

- Dodac `agentic_os/memory.py` z indeksowaniem SQLite FTS5 dla session
  summaries, model transcripts, bugs, decisions i learnings.
- Dodac komendy CLI `memory build` oraz `memory query <text>` scope'owane do
  aktywnego projektu.
- Wstrzykiwac skompresowane fragmenty prior-context na starcie sesji albo przy
  skladaniu promptu.

Akceptacja:

- `memory build` indeksuje historie jednego projektu i nie miesza projektow.
- `memory query` zwraca trafne, rankingowane fragmenty.
- Kontekst promptu/sesji miesci sie w skonfigurowanym budzecie.

Sugerowane checki:

```bash
.venv/bin/python -m pytest \
  tests/test_session_summary.py \
  tests/test_reasoning_transcripts.py \
  tests/test_learnings.py \
  tests/test_autonomy_cli_controls.py
./run-tests.sh
```

### Fala 7 - child work dla true unattended operation (#290)

Rekomendowany wzorzec brancha: `task/290-<child-slug>`

Wdrażac jako osobne child PR-y:

1. Automatyczna synteza taskow z wymagan, failure, crawl output i coverage
   gaps.
2. Per-phase checkpoint i resume, zeby retry fazy nie restartowal calego work
   itemu.
3. Deterministyczne auto-completion decyzji dla mechanicznie rozstrzygalnych
   gate'ow.
4. Proactive skill-failover recovery po reviewer REJECT, konsumujace
   `skill_failure` learnings z #287.
5. Predykcja kosztu i early abort zanim budzet sesji zostanie wyczerpany.

Akceptacja:

- Empty-queue full autonomy tworzy bounded actionable work albo zapisuje
  precyzyjny deterministyczny block.
- Mid-phase failures resume'uja tylko od nieudanej fazy.
- Mechaniczne decyzje auto-resolve'uja sie z audytowalnymi decision rows.
- Retry skill/provider jest ograniczony i nie omija reviewer gates.
- Predykcja kosztu emituje wczesny block przed wyczerpaniem budzetu.

Sugerowane checki:

```bash
.venv/bin/python -m pytest \
  tests/test_autonomy_preflight.py \
  tests/test_autonomy_step_outcome.py \
  tests/test_queue_policy_ordering.py \
  tests/test_provider_failover.py \
  tests/test_autopilot_verifications.py
./run-tests.sh
```

### Fala 8 - stabilnosc nawigacji dashboardu (#296)

Rekomendowany branch: `task/296-dashboard-nav-stability`

Zakres:

- Scentralizowac shell/nav dashboardu albo generowac go z jednego helpera.
- Usunac page-specific roznice szerokosci, spacingu, active-state albo sticky
  positioning nawigacji.
- Dodac screenshot albo structural tests porownujace x/y/width/height nawigacji
  na reprezentatywnych stronach desktop i narrow.

Akceptacja:

- Menu/nav nie zmienia pozycji miedzy subpage'ami dashboardu.
- Active, hover i focus nie zmieniaja rozmiaru linkow ani sasiadow.
- Screenshoty desktop i narrow pokazuja jeden spójny wzorzec shell.

Sugerowane checki:

```bash
.venv/bin/python -m pytest \
  tests/test_dashboard_ui_contracts.py \
  tests/test_dashboard_screenshots.py \
  tests/test_dashboard_browser_regression.py
```

### Fala 9 - dekompozycja zbyt duzych modulow (#292)

Rekomendowany wzorzec brancha: `task/292-split-<module>`

Zakres:

- Dzielic tylko jeden target module na PR: `routes/dashboard_server.py`,
  `cli.py`, `workflows/_legacy.py`, `gates.py`, `inbox.py` albo
  `autonomy.py`.
- Utrzymac stabilne public import paths albo zaktualizowac wszystkie call sites
  w tym samym PR.
- Traktowac to jako behavior-preserving cleanup; bez feature work w tym samym
  branchu.

Akceptacja:

- Pelny suite jest zielony po kazdym split.
- Brak zmian zachowania operator-facing, chyba ze jawnie opisane w issue/PR.
- Diff jest wystarczajaco maly, zeby zaudytowac jedna granice modulu naraz.

Sugerowane checki:

```bash
./run-tests.sh
git diff --check
```

### Fala 10 - wpiecie invoke_model w pipeline autonomii (#308)

Rekomendowany branch: `task/308-wire-invoke-model`

Prerequisite do re-scope dzieci 3-5 z #290. Petla autonomii to dzis
deterministyczna orkiestracja artefaktow: `analyze_work_item`,
`plan_work_item` i `implement_tests_for_work_item` buduja artefakty bez
wywolania modelu, `invoke_model` ma zero nie-testowych callerow, a jedyny
writer `model_invocations.cost_usd` (`models._invoke_attempt`) jest osiagalny
tylko przez niego, wiec koszt sesji w runtime jest zawsze zerowy. Dzieci 3-5
z #290 zakladaja in-process krok modelowy, ktorego nie ma.

Zakres:

- Wpiac `invoke_model` w kroki model-driven (minimum generacja planu, idealnie
  analiza) tak, by dzialaly in-process i zapisywaly wiersze
  `model_invocations` z `session_id`, `tokens_in/out` i `cost_usd`.
- Zachowac provider chain i semantyke failoveru `_rank_chain_by_quality`.
- Nie zregresowac deterministycznej orkiestracji dowiezionej przez dzieci 1-2.

Akceptacja:

- Sesja autonomii zapisuje wiersze `model_invocations` (koszt/tokeny/sesja)
  dla krokow model-driven.
- `budget_status` pokazuje niezerowy koszt sesji podczas realnego biegu.
- Po wejsciu tego: re-scope dzieci #290 3 (deterministic decision
  auto-completion), 4 (proactive skill-failover) i 5 (cost prediction + early
  abort) wzgledem realnej in-process powierzchni.

Sugerowane checki:

```bash
.venv/bin/python -m pytest \
  tests/test_model_invocation_wrappers.py \
  tests/test_autonomy_step_outcome.py \
  tests/test_session_summary.py
./run-tests.sh
```

## Runda 2 — Fale 11-16 (pelna nienadzorowana autonomia + redesign)

Fale 1-10 (#295, #291, #293, #288, #287, #289, #290, #296, #292, #308) sa
zmergowane. Repo to dzialajacy Agentic OS, ale `docs/rc-readiness-analysis.md`
ocenia je na 58/100 — BLOCK dla zewnetrznego RC. Runda 2 domyka droge do celu
koncowego: system autonomiczny, sterowalny z CLI **i** dashboardu, z metrykami i
monitoringiem, ktory buduje i **akumuluje** testy automatyczne dla jednego SUT —
per zadania gdy sa zadania, eksploracyjnie gdy ich brak — i nigdy nie zawiesza
sie na samej blokadzie. Potem dashboard dostaje pelny redesign wizualny.

Kazda fala to milestone GitHub z epic-issue i (dla wczesnych fal) child-issues.
Kolejnosc jest dependency-first: zachowanie, potem akumulacja, potem pipeline
operatorski, potem obserwowalnosc, potem CLI/config, na koncu warstwa wizualna.

### Fala 11 — Autonomia eksploracyjna online-only (epic #311)

Rekomendowany branch: `task/317-online-exploratory-default`

Naprawia zgloszona blokade online-only:
`idle:blocked — online web URL was crawled from sut.web.url, but empty-queue
task synthesis is deferred to issue #290`. Przyczyna: `autonomy.py:737-744`
zapisuje blokade, podczas gdy sciezka eksploracyjna (`_maybe_exploratory_baseline`,
autonomy.py:1146) jest zagatowana na `autonomy.exploratory_baseline` (domyslnie
off).

Children:
- #317 — domyslnie wlaczony exploratory baseline dla pustej kolejki online-only (P1).
- #318 — eventy exploratory baseline + komunikaty preflight/doctor.
- #321 — `[bug]` `task.html`/`decision.html` bez kanonicznego shell+nav
  (#296 zostawil te bare detail views nietkniete; menu wciaz znika/skacze na
  `/task/<id>`). Filed tutaj jako quick win Fali 11; pochloniete przez Fale 16.

Akceptacja: operator online-only, ktory ustawil tylko `sut.web.url`, dostaje
rosnacy zestaw testow eksploracyjnych bez kolejkowania zadania i bez edycji
flag; brak falszywego `idle:blocked`.

### Fala 12 — Akumulacja testow per SUT miedzy uruchomieniami (epic #312)

Rekomendowany wzorzec brancha: `task/312-<child-slug>`

Flagowe zachowanie. Akumuluj zestaw jednego SUT miedzy runami: nowe testy dla
nowych zadan, delta eksploracyjna gdy idle, nigdy duplikat. Bazuje na Fali 11 i
podpietym modelu z #308; reuzywa learnings #287 i per-project RAG #289.

Children:
- #319 — coverage ledger (trwaly rejestr pokrytych powierzchni per SUT).
- #320 — idempotentna generacja gatowana na ledger.
- (do zalozenia gdy fala ruszy) delta per-task; delta eksploracyjna.

Akceptacja: ponowny run na niezmienionym SUT dodaje zero duplikatow; nowa
trasa/zadanie dodaje dokladnie nowe pokrycie; ledger tlumaczy co jest pokryte.

### Fala 13 — End-to-end pipeline testowy RC (epic #313)

Rekomendowany wzorzec brancha: `task/313-<child-slug>`

Domyka luki RC 1, 2, 5, 6. Robi candidate → approve → generate → run → bug
jednym dowiedzionym flow na CLI i dashboardzie: operator-grade promocja
kandydatow, exact-spec failure → bug+evidence, hardening asercji fallback
(`needs_operator_decision`, nie ciche green) i jeden RC smoke test na fake SUT.

### Fala 14 — Kokpit metryk i monitoringu (epic #314)

Rekomendowany wzorzec brancha: `task/314-<child-slug>`

Zunifikowana obserwowalnosc. Rollupy `/api/metrics` (testy utworzone/odpalone,
pass/fail per powierzchnia, delta pokrycia, koszt/tokeny sesji — realne po #308,
failover rate, rozklad block-reason, czas per faza), jeden widok kokpitu
ewoluujacy wczesniejsze specy dashboardu (#193/#195/#196/#202) i opcjonalny
export Prometheus/JSON.

### Fala 15 — Kompletnosc CLI i prawdziwy sygnal gotowosci configu (epic #315)

Rekomendowany wzorzec brancha: `task/315-<child-slug>`

Domyka luki RC 4 i 7. Daemon orchestratora `up`/`down`, `logs --follow`,
`install-shim`, prawdomowny `doctor` wzgledem kanonicznego configu i wszystkich
rol modeli oraz dzialajacy scaffold przykladowego SUT.

### Fala 16 — Pelny redesign dashboardu (epic #316)

Rekomendowany wzorzec brancha: `task/316-<child-slug>`

Zaplanowana na koniec celowo — redesign przed ustabilizowaniem zachowania i
metryk to strata na przeróbki. Nowoczesna, profesjonalna, czytelna, efektywna
warstwa wizualna **na bazie** juz dostarczonych widokow (#244, #246, #247,
#266-#270, #191-#212): design system + offline tokeny (#200), jeden app shell
dla kazdej strony wlacznie z bare detail views, redesign per widok, responsive +
a11y, odswiezone baseline screenshotow.

## Globalne guardrails

- Jeden branch i PR na issue albo duzy child scope.
- Kazdy branch startuje ze swiezego `origin/main`; bez pushy i merge'y
  bezposrednio do `main`.
- Dla operator-facing docs synchronizowac twin EN/PL.
- Nie oslabiać asercji testow. Exact-spec failures zostaja czerwone i ida przez
  bug-aware flow.
- Zmiany dashboard/frontend wymagaja weryfikacji przegladarka albo screenshotem.
- Migracje storage wymagaja testow kompatybilnosci na istniejacej runtime DB.
- Zmiany autonomii musza emitowac audytowalne eventy dla kazdej autonomicznej
  decyzji, blokady, failoveru i zatrzymania budzetowego.

## Rekomendowany najblizszy branch

Fale 1-10 sa zmergowane. Runde 2 zaczac od `task/317-online-exploratory-default`
(#317, Fala 11). Usuwa zgloszony stan online-only `idle:blocked` — system obecnie
odmawia zrobienia czegokolwiek uzytecznego skonfigurowany tylko z web URL — i
zamienia "brak zadan" na generacje testow eksploracyjnych, co jest warunkiem
wstepnym dla zachowania akumulacji z Fali 12. Sparowac z tanim bugiem
nav-consistency #321 w tej samej fali.
