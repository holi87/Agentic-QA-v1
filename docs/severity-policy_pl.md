# Polityka severity

Status: active

Kontrakt projektowy fazy 06. Definiuje wartości `severity` używane w
tabelach `bugs`, `blockers` i `decisions`, w dashboardzie oraz w każdym
prompcie rozstrzygającym niepowodzenia.

Powiązania: `docs/bug-aware-policy.md`,
`docs/database-schema.md`, `docs/standards/bug-reporting.md` (po migracji
w fazie 07), `.qualitycat/prompts/bug-adjudication.md`.

## 1. Skala severity

Cztery dyskretne wartości. Nie dodawaj pośrednich; constraint CHECK w
bazie wymusza ten zbiór.

| Kod | Etykieta | Definicja |
|---|---|---|
| `P0` | Critical | SUT nieużywalny, dowód utraty danych, obejście uwierzytelnienia, naruszenie bezpieczeństwa lub każdy defekt, który zatrzymałby sam konkurs gdyby znaleziony w produkcji. |
| `P1` | High | Złamany główny flow biznesowy, błędna suma/stan w happy-path, brak walidacji na krytycznym polu, dowód regresji w wielu scenariuszach. |
| `P2` | Medium | Istnieje obejście, złamany flow drugorzędny, błędny kod błędu w edge case, obserwowalne, ale nieblokujące naruszenie specyfikacji. |
| `P3` | Low | Kosmetyczne lub marginalne: literówka w etykiecie, off-by-one w niekrytycznym liczniku, problem z formatowaniem, nieudokumentowany edge case. |

`Info` (niejednoznaczność specyfikacji) ze starego standardu mapuje się
na wiersz `blockers` z `severity='P2'`, `source='requirements_clarification'`.
Nigdy nie jest wierszem `bugs`.

## 2. Severity = Impact × Likelihood

Użyj poniższej macierzy; wybierz najgorszą komórkę, którą scenariusz
rozsądnie wywołuje.

| Impact ↓ / Likelihood → | High (happy path) | Medium (specific input) | Low (edge combo) |
|---|---|---|---|
| Catastrophic (data loss, auth bypass) | P0 | P0 | P1 |
| Major (broken business flow)         | P1 | P1 | P2 |
| Minor (workaround available)         | P2 | P2 | P3 |
| Cosmetic                              | P3 | P3 | P3 |

W razie wątpliwości między dwoma sąsiednimi koszykami wybierz wyższy
severity w trakcie konkursu i pozwól operatorowi zdegradować przy review.

## 3. Reguły routingu

Używane przez orkiestrator i `qualitycat.file_bug`:

| Severity | Auto-file bug | Przerwij operatora | Blokuj cut fazy | SLA potwierdzenia |
|---|---|---|---|---|
| P0 | tak | tak, natychmiast | tak | w ciągu 1 min |
| P1 | tak | tak, ≤5 min | tak jeśli wciąż otwarty w cut | w ciągu 5 min |
| P2 | tak | nie (tylko kolejka) | nie | koniec fazy |
| P3 | tak | nie | nie | koniec konkursu |

Jeśli budżet przerwań operatora w godzinie (4 wg §7
`docs/bug-aware-policy.md`) jest wyczerpany, P1 nadal przerywa po
przejściu godziny, a P2/P3 zostają w kolejce.

## 4. Kiedy pytać operatora (a nie tylko zgłaszać)

Pytaj, nie auto-file'uj, gdy zachodzi KTÓRYKOLWIEK z warunków:

1. Źródła niepowodzenia nie da się powiązać z klauzulą wymagania (brak
   referencji w `requirements.md`, brak ścieżki OpenAPI).
2. Ten sam scenariusz testowy ma już 2 odrębne wiersze `bugs` w tym
   konkursie — wskazuje na flaky test lub ruchomy cel.
3. Patch w review osłabiałby asercję (patrz `docs/bug-aware-policy.md` §6).
4. Blocker ma severity P0 lub P1 i orkiestrator nie ma automatycznej
   remediacji w bieżącej fazie.

Operator odpowiada przez wiersz `decisions`. Orkiestrator zapisuje
`decided_by='operator'`, temat, rationale i consequences.

## 5. Propagacja severity

- `bugs.severity` kaskaduje do tagów scenariusza testowego
  (`@severity-P0` … `@severity-P3`) przy przetagowaniu w §2
  `bug-aware-policy.md`.
- `blockers.severity` steruje kolorem dashboardu (`P0` czerwony, `P1`
  bursztynowy, `P2/P3` neutralny) i tym, czy `recovery_scan` podnosi
  stan runtime do `degraded`.
- `decisions.severity` jest niejawne — decyzja powiązana z bugiem P0
  jest sama P0 do celów cut.

## 6. Interakcja ze scope cut

Decyzja cut fazy (`VERIFY_TRIAGE`) MUSI uwzględnić otwarte severity:

- Każdy otwarty P0 wymusza odpowiedź cut "block ship until resolved or
  operator acknowledges risk".
- Każdy otwarty P1 wymusza jawny wiersz `decisions` przed ship
  (`topic='ship_with_open_P1'`).
- P2 i P3 nie wymagają dodatkowej decyzji, ale pojawiają się w
  podsumowaniu gotowości.

## 7. Workflow degradacji severity

Severity można zmniejszyć tylko gdy:

1. Operator zapisze wiersz `decisions` z
   `topic='severity_downgrade'`, podając id buga i nowy severity.
2. UPDATE `bugs.severity` dzieje się w tej samej transakcji co INSERT
   `decisions`, by ślad audytowy nie odjechał.

Automatyczne degradacje są zabronione.
