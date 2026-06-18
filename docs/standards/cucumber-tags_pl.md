# Konwencja tagów Cucumber

Status: active

QualityCat używa warstwowego schematu tagów. `QC-claude-init` interaktywnie
decyduje o tagach business-area specyficznych dla projektu. Tagi lifecycle
poniżej są obowiązkowe.

## Obowiązkowe tagi lifecycle

Istnieją w każdym projekcie i mają dedykowane runnery.

| Tag | Cel | Docelowy czas wykonania |
|---|---|---|
| `@healthcheck` | Weryfikuje dostępność środowiska / SUT przed właściwym testem. Czysty puls infrastruktury. | < 10s |
| `@smoke` | Top 3-5 krytycznych happy-paths potwierdzających, że SUT żyje. | < 60s |
| `@critical` | Top cele biznesowe — muszą zawsze przechodzić na zielonym buildzie. | < 5 min |
| `@regression` | Każdy scenariusz dostaje ten tag. Pełny suite. | zależy |
| `@negative` | Udokumentowane odpowiedzi błędów (401, 403, 404, 422, …). | zależy |
| `@boundary` | Brzegowe wartości liczbowe / string / data dla krytycznego inputu. | zależy |
| `@security` | Mapowania OWASP API Top 10 i OWASP Top 10. | zależy |
| `@extended` | Runda 2 — warianty parametryczne, głębokie pokrycie. Opcjonalne w panic mode. | zależy |
| `@known-bug` | Sparowany z `@bug-NNN`. Scenariusz oczekiwany do oblania, dopóki aplikacja nie zostanie naprawiona. | zależy |

## Tagi business-area specyficzne dla projektu

Decydowane podczas `QC-claude-init`. Format: `@functional-<area>` (kebab-case, małe litery).

Przykłady: `@functional-auth`, `@functional-users`, `@functional-orders`,
`@functional-billing`, `@functional-search`.

Każdy scenariusz MUSI mieć dokładnie jeden tag `@functional-<area>`.

## Specjalne sub-tagi

- `@security-a11y` — kontrole accessibility (WCAG 2.2). Odrębne od `@security`.
- `@owasp-api1` … `@owasp-api10` — mapowanie konkretnej pozycji OWASP API Top 10.
- `@owasp-a01` … `@owasp-a10` — mapowanie OWASP Web Top 10 (2021).
- `@bug-NNN` — paruje się z `@known-bug`, referuje plik `bugs/BUG-NNN-<slug>.md`.
- `@reference` — rezerwuje scenariusz jako wzorzec referencyjny; nigdy nie wykonywany
  przez domyślne runnery.

## Wymagane kombinacje

Każdy scenariusz MUSI mieć:
- dokładnie jeden tag `@functional-<area>`,
- ORAZ co najmniej jeden z: `@smoke`, `@critical`, `@regression`,
- ORAZ `@regression` (pokrywa wszystko dla pełnych runów suite).

Gdy to zasadne, dodaj również:
- `@negative` dla udokumentowanego kodu błędu,
- `@boundary` dla edge'y liczbowych/string/data,
- `@security` (z sub-tagiem `@owasp-*`) dla scenariuszy security,
- `@known-bug @bug-NNN` jeśli scenariusz jest spięty ze znanym defektem.

## Przykłady CLI

```bash
# Run by lifecycle tag
./gradlew test -Dcucumber.filter.tags="@healthcheck"
./gradlew test -Dcucumber.filter.tags="@smoke"
./gradlew test -Dcucumber.filter.tags="@critical and not @extended"

# Run by business area
./gradlew test -Dcucumber.filter.tags="@functional-users"
./gradlew test -Dcucumber.filter.tags="@functional-auth or @functional-users"

# Run security suite
./gradlew test -Dcucumber.filter.tags="@security"

# Run all OWASP API1 (IDOR) scenarios
./gradlew test -Dcucumber.filter.tags="@owasp-api1"

# Skip known bugs (clean green run)
./gradlew test -Dcucumber.filter.tags="not @known-bug"

# Combine
./gradlew test -Dcucumber.filter.tags="@functional-orders and @critical and not @known-bug"
```

## Anty-wzorce

- Eksplozja tagów (50+ unikalnych) → konsoliduj.
- Literówki w wielkości liter (`@Smoke`, `@CRITICAL`) → wymuś lowercase.
- Wiele tagów `@functional-*` na jednym scenariuszu → rozdziel scenariusze.
- `@known-bug` bez `@bug-NNN` → albo usuń tag, albo dodaj prawdziwy BUG ID.
- `@critical` na każdym scenariuszu → krytyczność traci znaczenie. Zarezerwuj dla top celów biznesowych.

## Ostatnia aktualizacja

2026-05-08.
