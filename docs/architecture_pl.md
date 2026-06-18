# Architektura

Status: aktywny. Napisany dla issue #293 i zweryfikowany względem kodu w
`scripts/agentic-os/agentic_os/`. To kanoniczna mapa architektury. Blok
caveman-compressed przy końcu to forma wstrzykiwana do promptów agentów
(`models._invoke_attempt`); ten czytelny dokument pozostaje źródłem prawdy,
a blok aktualizuj w tym samym commicie. Forma wstrzykiwana do promptów używa
wersji angielskiej (`docs/architecture.md`).

## Mapa modułów

Runtime to pakiet `agentic_os` pod `scripts/agentic-os/`.

- **Wejście / orkiestracja**: `cli.py` (CLI operatora), `orchestrator.py`
  (cykl życia work-item + run), `autonomy.py` (pętla sesji bezobsługowej),
  `scheduler.py` (odpalanie harmonogramów cron), `queue.py` (kolejność kolejki
  + szacunki tokenów).
- **Dashboard**: `routes/dashboard_server.py` (serwer HTTP loopback; `server.py`
  to cienki alias), `dashboard.py` (buildery widoków), `templates/`.
- **Modele**: `models/__init__.py` — `invoke_model` (publiczne) → `_invoke_attempt`
  (jeden chokepoint składający prompt i uruchamiający CLI dostawcy),
  `models/providers/` (parsowanie per-dostawca + sufiks koperty). Skille i
  kontekst architektury są splice'owane tutaj.
- **Składanie promptu**: `skills.py` (`compose_prompt` doklejający skille per
  rola), `architecture_context.py` (wstrzykuje skompresowane podsumowanie
  architektury).
- **Workflows**: `workflows/` — kroki analyze, plan, implement-tests,
  review-gate, run-tests, final-gate; `runner.py` opakowuje
  `runtime/subprocess.py` rekordami run + manifestami.
- **Granica podprocesu**: `runtime/subprocess.py` (jedyna ścieżka do komend
  zewnętrznych; tylko argv, wyselekcjonowana PATH, allowlista env, redakcja
  logów), `security.py` (`require_safe_argv`, `resolve_repo_path`, redakcja).
- **SUT**: `sut_lifecycle.py` (compose up/down, healthcheck), `sut_discovery.py`,
  `sut_repo.py`, `exploratory.py` (runner baseline), `crawler*.py`.
- **Flow jakości**: `gates.py` (gate review/final), `learnings.py` (doradcze
  learnings + decay), `decisions.py`, `results.py` / `triage_classifier.py`
  (triage bugów), `qualitycat.py` (fasada skryptów QA), `coverage_review.py`.
- **Storage**: `storage/db.py` (połączenie WAL + runner migracji),
  `storage/schema.sql`, `budgets.py` (agregacja tokenów + `estimate_tokens`),
  `events.py` (append-only log zdarzeń), `paths.py` (`RuntimePaths`).
- **Generatory / planowanie**: `generators/` (generacja testów API + UI),
  `plan_v2.py`, `test_planning.py`, `openapi.py`.

## Model pracy: project → work_item → phase → task

- **work_item** to jedna jednostka pracy QA (tworzona z task spec, ingestu
  inbox lub autonomii). Issue #288 doda adresowalną warstwę **project** nad
  work_items; dziś runtime jest single-SUT.
- **phases** to zasiane etapy pipeline'u; work_item przechodzi przez fazy kroku
  `analyze → implement → review → triage` (zob. `_ROLE_TO_STEP_PHASE`).
- **tasks** / **runs** zapisują pojedyncze wykonania; wiersze `runs` parują się
  z logami podprocesów i manifestami. `leases` strzegą współbieżnego
  posiadania work-item.
- Zależności między work_items żyją w `work_item_deps`; wytworzone pliki w
  `work_item_artifacts`.

## Tabele DB runtime

SQLite, WAL, `storage/schema.sql`, `SCHEMA_VERSION = 13` (migracje w
`storage/db.py`). Tabele główne:

`work_items`, `work_item_deps`, `work_item_artifacts`, `tasks`, `runs`,
`phases`, `leases`, `events`, `event_offsets`, `model_invocations`,
`model_transcripts`, `learnings`, `decisions`, `blockers`, `bugs`, `evidence`,
`test_results`, `assertion_changes`, `autonomy_sessions`, `provider_cooldowns`,
`schedules`, `session_bookmarks`, `schema_migrations`.

## Wiązanie ról modeli

Cztery role, każda z głównym dostawcą + łańcuchem failover (`config/agentic-os.yml`):

| Rola | Główny | Failover | Faza kroku |
|---|---|---|---|
| planner | claude/opus | codex, antigravity/gemini | analyze |
| implementer | claude/sonnet | codex, antigravity/gemini | implement |
| reviewer | codex | claude/sonnet, antigravity/gemini | review |
| triager | claude/haiku | codex, … | triage |

Wszystkie cztery przechodzą przez `models._invoke_attempt`, który: składa
prompt (kontekst architektury + skille + zadanie bazowe + sufiks koperty),
uruchamia CLI dostawcy przez `runtime.subprocess`, redaguje wejście i parsuje
kopertę dostawcy. Failover re-rozwiązuje skille per-dostawca.

## Flow gate / learnings / memory

- **Gates** (`gates.py`): `static_review_gate` na diffach; wyjście reviewera
  parsowane przez `parse_reviewer_invocation` → `parse_gate_output` (ścisła
  koperta APPROVE/REJECT); `final_gate` / `evaluate_final_gate` decydują o
  gotowości do wydania. Zmiany asercji wymagają wiersza decyzji; porażki
  exact-spec otwierają bugi.
- **Learnings** (`learnings.py`): `record_learning` zapisuje doradcze wiersze;
  `decay_learnings` postarza je przez `decayed_weight`; `provider_quality_scores`
  i `flaky_subjects` informują routing. Zapisy są best-effort — porażka
  learning nigdy nie psuje flow hosta. Wstrzykiwanie do promptów to issue #287.
- **Memory**: per-project RAG memory to issue #289 (planowane); zaindeksuje
  podsumowania sesji / transkrypty / bugi i będzie dzielić budżet
  wstrzykiwanego kontekstu z kontekstem architektury i learnings.

## Wstrzykiwany kontekst agenta

`architecture_context.py` czyta blok skompresowany poniżej, ogranicza go do
budżetu tokenów (`prompt_context.architecture_budget_tokens`, domyślnie 600),
a `models._invoke_attempt` doklejá go przed skillami. Wstrzykiwanie jest
best-effort: brakujący/wadliwy blok emituje `architecture.injection_failed` i
wywołanie idzie dalej bez niego. Blok musi pozostać wolny od literałów
wyglądających na sekrety (inaczej redakcja `security.py` by go zniekształciła).

**Delta tokenów** (`budgets.estimate_tokens`, ~4 znaki/token): pełny czytelny
dokument to ~1700 tokenów; wstrzykiwane skompresowane podsumowanie to ~291
tokenów (~315 z opakowaniem promptu) — redukcja ~83% względem wysyłania całego
dokumentu, wygodnie wewnątrz domyślnego budżetu 600 tokenów. Pozostały zapas
budżetu zostaje na wstrzyknięcia #287 (learnings) i #289 (memory), aby trójka
nie stackowała się poza ograniczonym budżetem kontekstu promptu.

<!-- inject:architecture-summary:start -->
Agentic OS = lokalny runtime agentów QA (pakiet `agentic_os`). Jednostka pracy
= work_item; przechodzi fazy analyze -> implement -> review -> triage. Role:
planner (claude/opus), implementer (claude/sonnet), reviewer (codex), triager
(claude/haiku); każda ma łańcuch failover. Wszystkie wywołania modeli idą przez
`models._invoke_attempt`: prompt = kontekst-arch + skille per-rola + zadanie +
sufiks koperty; CLI dostawcy uruchamiane przez `runtime.subprocess` (tylko
argv, wyselekcjonowana PATH, allowlista env, redakcja logów). Stan = SQLite WAL
(`storage/schema.sql`, wersja 13): work_items, runs, tasks, phases, leases,
events, model_invocations, learnings, decisions, bugs, evidence, test_results,
autonomy_sessions. Gates (`gates.py`): reviewer emituje ścisłą kopertę
APPROVE/REJECT; final_gate decyduje o wydaniu; zmiany asercji wymagają wiersza
decyzji; porażki exact-spec otwierają bugi. Learnings (`learnings.py`) doradcze
+ decayed, best-effort, nigdy nie blokują flow hosta. Dashboard = tylko
loopback (`routes/dashboard_server.py`), metody modyfikujące wymagają
host+origin+token. Komendy SUT działają bez sandboxa, ale bez poświadczeń
dostawców. Kontrakt wyjścia: emituj kopertę roli; nie streszczaj wczytanych
skilli.
<!-- inject:architecture-summary:end -->
