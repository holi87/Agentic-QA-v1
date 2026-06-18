# Polityka Bug-Aware

Status: active

Kontrakt projektowy fazy 06. Reguły poniżej są normatywne dla orkiestratora,
wszystkich wrapperów runtime i każdego promptu review. Implementacje, które
zmieniają asercje testowe, aby czerwony test zazielenić, naruszają tę
politykę i muszą oblać dotknięty gate.

Powiązania: `docs/severity-policy.md`, `docs/standards/bug-reporting.md`
(po migracji w fazie 07), `docs/runtime-contract.md`,
`scripts/assertion-guard.py`, gate'y w `.qualitycat/agentic-os.yml`
(`exact_spec_failure_opens_bug`, `assertion_changes_require_decision`,
`known_bugs_fail_exit`).

## 1. Hierarchia źródła prawdy

Gdy SUT i dokumentacja są niezgodne, polityka rozstrzyga konflikt w
ustalonej kolejności:

1. **Zablokowana decyzja operatora** zapisana w tabeli `decisions`.
2. **Wymagania biznesowe** w `requirements.md` (lub specyfikacja źródłowa).
3. **Kontrakt OpenAPI / interfejs** dostarczony z SUT.
4. **Zaobserwowane zachowanie SUT**.

Ilekroć punkty 2 i 3 są niezgodne, orkiestrator musi otworzyć blocker
`decision` zanim pozwoli powiązanemu testowi po cichu przejść lub oblać się.

## 2. Reguła exact-spec failure

Scenariusz jest *exact-spec failure*, gdy:

- Pochodzi z pliku feature mapowanego 1:1 na klauzulę wymagania, oraz
- Komunikat o niepowodzeniu wskazuje asercję kodującą tę klauzulę.

Dla każdego exact-spec failure orkiestrator MUSI:

1. Pozostawić asercję bez zmian.
2. Uruchomić `qualitycat.file_bug(...)`, który tworzy
   `bugs/BUG-NNN-<slug>.md`, katalog evidence pod `agentic-os-runtime/evidence/`
   i wstawia wiersz `bugs` ze `status='open'`.
3. Przetagować scenariusz `@known-bug @bug-NNN` i dodać go do
   `bugs/README.md` (otwórz plik w trybie append, nigdy nie nadpisuj).
4. Utrzymać exit code `run-tests.sh` na `1` (gate
   `known_bugs_fail_exit: true`).
5. Wyemitować zdarzenie `bug.filed` z payloadem
   `{bug_id, severity, scenario, requirement_ref}`.

Nigdy nie usuwaj `@known-bug` bez jawnej decyzji człowieka zapisanej w
`decisions` (`decided_by='operator'`).

## 3. Konflikt: OpenAPI vs wymaganie biznesowe

Gdy schemat OpenAPI jest sprzeczny z wymaganiem biznesowym:

1. Implementer (Sonnet) NIE generuje testu wyłącznie z OpenAPI.
2. Orkiestrator otwiera blocker `decision` (severity wg sekcji 4
   `docs/severity-policy.md`) z `source='requirements_vs_openapi'`.
3. Operator odpowiada przez wiersz `decisions`. Orkiestrator zapisuje
   `decided_by`, `rationale`, `consequences`.
4. Test jest generowany z wybranego źródła i odwołuje się do
   `decision_id` w docstringu/komentarzu.

Jeśli decyzja nie nadejdzie w budżecie konkursu, orkiestrator domyślnie
przyjmuje *wymaganie biznesowe* i oznacza scenariusz testowy
`@requires-decision`, nigdy fałszywie zielony.

## 4. Routing wg severity

| Severity | Auto-file bug | Przerwij operatora | Blokuj cut fazy |
|---|---|---|---|
| P0 | tak | tak (natychmiast) | tak |
| P1 | tak | tak (w ciągu 5 min) | tak jeśli otwarty w cut |
| P2 | tak | nie | nie |
| P3 | tak (niski priorytet) | nie | nie |

Szczegółowe definicje severity znajdują się w `docs/severity-policy.md`.
Orkiestrator MUSI sprawdzić `gates.assertion_changes_require_decision` oraz
`gates.exact_spec_failure_opens_bug` przed działaniem.

## 5. Polityka scope cut (okno 5h)

Wywołaj cut fazy `VERIFY_TRIAGE`, gdy spełniony jest KTÓRYKOLWIEK z warunków:

1. ≥4 otwarte blockery o severity ≥ P2.
2. Pozostały budżet konkursu < 75 minut ORAZ żaden obszar feature API nie
   ma zielonego pokrycia.
3. ≥3 kolejne uruchomienia `IMPLEMENT` zwróciły `failure_kind='infra'`.
4. Dashboard raportuje `bugs.open + blockers_open > 8`.

Na `VERIFY_TRIAGE`:

- Zawieś nowe zadania implementacyjne (`IMPLEMENT`, `DESIGN`).
- Sfinalizuj raporty dla wszystkiego, co jest zielone lub już zgłoszone.
- Uruchom `qualitycat.copy_reports` + `qualitycat.build_summary`.
- Otwórz końcowy blocker `decision` `severity=P0` pytający operatora,
  czy wysyłać.

## 6. Gate niezmienności asercji

`scripts/assertion-guard.py` to kanoniczne egzekwowanie. Powtórzone dla
tej polityki:

- Każdy patch osłabiający asercję (zamiana regex, poszerzenie zakresu,
  wstawienie `assertTrue(true)`, zredukowanie `expect(...).toBeDefined()` do
  `expect(...).toBeTruthy()` itp.) jest ODRZUCANY, chyba że istnieje
  wiersz `assertion_changes` ze `status='allowed'` i linkowanym
  `decisions.id`.
- Wzmocnienie (węższa wartość oczekiwana, ściślejszy regex) jest dozwolone.
- Orkiestrator zapisuje każdą wykrytą zmianę jako wiersz
  `assertion_changes` niezależnie od wyniku; ten wiersz to ślad audytowy
  dla gate'ów review.

Nie istnieje ścieżka polityki pozwalająca zmienić asercję *tylko* po to,
by czerwony test zazielenić. Patche robiące to muszą być ODRZUCONE przez
Codex review (prompt: `.qualitycat/prompts/codex-reviewer.md`).

## 7. Budżet przerwań operatora

Twardy limit: maks. 4 przerwania na godzinę konkursu. Jeśli kolejka go
przekracza, zdegraduj severity do P2 i kontynuuj zgłaszanie bugów zamiast
pytać. Resetuj licznik co godzinę. Zawsze przerywaj dla P0.

## 8. Działania zabronione

Orkiestrator MUSI odrzucić (z `error_class='policy_violation'`) każde
zadanie, które prosi o:

- Modyfikację plików źródłowych SUT (dowolna ścieżka pod `sut.root` wg config).
- Usunięcie tagów `@known-bug` lub `@bug-NNN` z testu green-on-red.
- Pominięcie generowania raportu gdy `reports.require_reports_on_failure=true`.
- Promocję testu do green poprzez zmianę asercji, gdy otwarty wiersz
  `bugs` referuje ten scenariusz.

Każde odrzucone zadanie emituje zdarzenie `policy.violation_rejected` i
zapisuje wiersz w `blockers` z `severity='P1'`, `source='policy'`.
