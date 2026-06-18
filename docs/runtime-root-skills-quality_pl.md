# Utwardzenie runtime root i jakosci skilli

Status: active

Data: 2026-05-22.

## Decyzja architektoniczna

Agentic OS uzywa teraz `agentic-os-runtime/` jako domyslnego runtime root.
Katalog jest celowo widoczny, bo operator musi latwo sprawdzac SQLite state,
eventy, plany, wygenerowane task specy, model IO i triage runow bez walki z
ukrytymi folderami w file pickerach, dashboardzie albo screenshotach.

Legacy `.agentic-os/` zostaje wspierane w dwoch przypadkach:

- operator jawnie zostawia `runtime.root: .agentic-os` w configu;
- configu brakuje, a `.agentic-os/` jest jedynym istniejacym katalogiem runtime.

Wszystkie nowe init/config sciezki powinny uzywac `agentic-os-runtime/`.

## Wdrozone

- Helpery sciezek runtime domyslnie wskazuja `agentic-os-runtime/`.
- `open_runtime()`, `up`, `status`, `logs`, `doctor`, `inbox list` i dashboard
  uzywaja runtime root z configu zamiast hardcoded `.agentic-os/`.
- Przyklady configu i sledzony lokalny config maja `runtime.root:
  agentic-os-runtime`.
- `.gitignore`, dokumenty operatorskie, copy dashboardu i kontrakty runtime
  nazywaja widoczny runtime root.
- `/files/...` dalej dziala przez allowliste i dalej blokuje prywatne
  `state.db`.
- Skille `init-project` nie odwoluja sie juz do nieistniejacych template,
  legacy skill-directory ani root-standard assets.
- Skille planner/implementer wymagaja Candidate Quality Contract: dokladna
  asercja, target surface, wartosc biznesowa, dane testowe, cleanup strategy,
  functional tag, lifecycle tag i source reference.

## Standard jakosci dla kolejnych zmian w skillach

Skille providerowe sa fragmentami promptu, nie samodzielnymi globalnymi
skillami. Nie moga wymyslac assetow, ktorych repo nie dostarcza. Gdy brakuje
metadanych albo scaffoldu, powinny zatrzymac sie z maszynowo czytelnym
`needs_input: <field>` zamiast produkowac plytki albo fikcyjny output.

Eksploracja publicznej strony nie moze konczyc sie na jednym lub dwoch smoke
checkach, chyba ze task jawnie tak ogranicza zakres. Minimalne sensowne
pokrycie obejmuje odkrywanie tras/linkow, reprezentatywne strony, asset checks,
console errors, accessibility basics i co najmniej jedna biznesowo widoczna
asercje per reprezentatywny flow.

## Dalsze follow-upy

- Zbudowac realny route crawler/generator, zeby szerokosc public-site testow
  wynikala z same-origin discovery, nie tylko z tekstu taska.
- Dodac rendered dashboard browser regression, gdy w sesji bedzie dostepne
  narzedzie Browser Node REPL.
- Dodac pelnoprawny demo SUT/test scaffold, jesli `init-project` ma tworzyc
  wykonywalne projekty Java/Playwright z pustego katalogu bez wyboru stacka
  przez operatora.
