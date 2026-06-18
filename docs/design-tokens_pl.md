# Tokeny designu dashboardu

Pojedyncze źródło prawdy dla warstwy wizualnej dashboardu operatora
(epic #316 / Wave 16). Każda decyzja dotycząca koloru, odstępu,
typografii i powierzchni żyje w
`scripts/agentic-os/templates/static/dashboard.css` pod właściwością
CSS `--token-name`. Szablony **nie mogą** wprowadzać nowych
zahardkodowanych kolorów, krojów pisma ani wypełnień — konsumują
tokeny.

Architektura jest dwutorowa:

- **Ethereal Glass** — tryb ciemny. Półprzezroczyste powierzchnie,
  delikatne kreseczki, subtelna radialna zasłona; teksty o wysokim
  kontraście.
- **Soft Structuralism** — tryb jasny (domyślny). Ustrukturyzowane
  kremowo-białe powierzchnie, stonowany kolor marki, spokojne
  kreseczki.

Oba używają tych samych nazw tokenów. Blok `:root` deklaruje wartości
trybu jasnego; `@media (prefers-color-scheme: dark)` je przełącza.
`color-scheme: light dark` sprawia, że natywne kontrolki formularza
podążają za trybem.

## Rodziny tokenów

| Rodzina      | Tokeny                                                                                       | Uwagi                                              |
|--------------|----------------------------------------------------------------------------------------------|----------------------------------------------------|
| Powierzchnia | `--bg`, `--bg-veil`, `--surface`, `--surface-strong`, `--surface-inset`                       | Tła + wypełnienia kart. `--bg-veil` to potrójny radialny gradient. |
| Obramowanie  | `--border`, `--border-strong`, `--ring-outer`, `--ring-inner`, `--hairline-grad`              | Kreseczki i cienie pierścieni.                     |
| Tekst        | `--text`, `--text-soft`, `--text-muted`, `--text-faint`                                       | Cztery poziomy: treść, drugorzędne, meta, skeleton. |
| Marka        | `--primary`, `--primary-strong`, `--primary-soft`, `--focus-ring`                              | Jeden kolor marki + maska focusa.                  |
| Stan         | `--state-ready`, `--state-running`, `--state-degraded`, `--state-blocked`                     | Mapują plakietki statusu autonomii / zadań na jedną paletę. |

## Typografia

Tylko lokalne/offline (issue #200 — żadne zdalne `@import`). Nazwy
krojów pozostawione w stosie, żeby operatorzy z zainstalowanymi
czcionkami otrzymywali zaprojektowaną tożsamość; reszta wraca do stosu
platformowego:

- Sans: `Geist, ui-sans-serif, system-ui, -apple-system, Segoe UI, …`
- Mono: `'Geist Mono', ui-monospace, SFMono-Regular, …`
- Serif: `'Instrument Serif', ui-serif, Georgia, …` (oszczędnie, w
  nagłówkach hero / placeholderach "no data").

## Powłoki układu

Dashboard ma dwie odmiany powłoki:

1. **Standardowa powłoka** (większość stron) — `<header class="topbar">`
   zawiera kanoniczny nav wstrzykiwany przez `<!-- DASHBOARD_NAV -->`.
   Handler w `dashboard_server.render_nav` jest jedynym źródłem
   członkostwa w nav, więc brakujący wpis ujawnia się w jednym miejscu,
   nie w siedmiu.
2. **Kompaktowa powłoka detalu** — `<body class="detail-shell">` na
   `task.html` i `decision.html`. Dziedziczy topbar/nav, ale zawęża
   padding `main` i używa siatki definition-list `.meta`. Zdefiniowana
   pod `.detail-shell { … }` w dashboard.css — zobacz sekcję "Wave 16".

## Klasy narzędziowe układu (dodane przez #316)

| Klasa             | Cel                                                                                    |
|-------------------|----------------------------------------------------------------------------------------|
| `.row-flex`       | Wiersz `display: flex; gap: 8px; align-items: center` — zastępuje wstrzyknięte `style=""`. |
| `.row-flex--wrap` | Modyfikator zawijania na wąskich ekranach.                                             |
| `.grid-2`         | Siatka dwukolumnowa z równymi torami.                                                  |

## Dodawanie nowych tokenów

1. Zadeklaruj w `:root` (jasny) i w bloku `@media (prefers-color-scheme: dark)`,
   żeby tryb ciemny nigdy nie wracał do bieli.
2. Używaj przez `var(--token, fallback)`, żeby brakujący token w
   runtime nie psuł czytelności strony.
3. Odwołuj się, nie deklaruj na nowo w `<style>` szablonu — te bloki
   powinny być puste; reguły specyficzne dla szablonu żyją w
   dashboard.css pod klasą z przestrzenią nazw.

## Odświeżanie baseline'ów screenshotów

Zmiany wizualne, które przechodzą lokalny suite, często przesuwają
linuxowe baseline'y powyżej 2% progu pixel-diff. Po zalądowaniu:

```bash
# poczekaj na CI, potem
gh run download <run-id> -n dashboard-screenshots -D /tmp/shots
cp /tmp/shots/*.png tests/snapshots/dashboard/linux/
git add -A && git commit -m "test: refresh dashboard baselines for <change>"
```

Zobacz `tests/snapshots/dashboard/linux/README.md` po kanoniczne
zasady proweniencji.

## Anty-wzorce

- Zahardkodowany hex poza dashboard.css — użyj tokena.
- Nowy blok `<style>` w szablonie — rozszerz dashboard.css pod klasą z
  przestrzenią nazw.
- Ustawianie `style="..."` na układzie — użyj `.row-flex` / `.grid-2`
  lub dodaj nową klasę narzędziową.
- Pobieranie zdalnych czcionek (Google Fonts itd.) — tylko stos
  lokalny (#200).
- Zmiana nazwy tokena bez aliasu in-place — stare nazwy klas
  referencowane przez JS po cichu by się rozjechały.
