# Analiza przeplywu operatorskiego dashboardu

Status: superseded
Superseded-on: 2026-05-22
Superseded-by: operator-guide_pl.md, dashboard-help.md
Reason: point-in-time analiza z 2026-05-21. Wszystkie "Implemented changes"
wjechały na `main` (rozszerzenie allowlisty `/files`, bulk approve,
analiza świadoma surface'ów, parsowanie URL/route, głębokość eksploracji
skilli). Traktuj jako zapis uzasadnienia; aktualne zachowanie opisane jest
w `operator-guide_pl.md` i `dashboard-help.md`.

Data: 2026-05-21.

Zakres: przeplyw dashboardu dla zadania na publicznej stronie online, z
`sut.web.enabled=true` i `sut.api.enabled=false`, gdzie CLI potrafi isc dalej,
a dashboard blokuje operatora albo pokazuje mylace kandydaty.

## Ustalenia

### 1. Linki do artefaktow byly niedostepne z dashboardu

Dashboard renderowal linki do artefaktow operatorskich w katalogach runtime:

- `.agentic-os/analysis/<task>/requirements.md`
- `.agentic-os/plans/<task>/TEST-PLAN.md`
- `.agentic-os/task-specs/<task>.md`
- `.agentic-os/runs/<run>/triage.md`

`server.py` obslugiwal `/files/...` przez scisla allowliste, ale ta allowlista
obejmowala tylko reports, bugs, evidence, patches i logi subprocess. Linki do
analysis, plans, task specs i runs zwracaly wiec 404.

Ukryty katalog z kropka nie byl bezposrednia przyczyna. Problem byl po stronie
allowlisty serwera, nie po stronie widocznosci katalogu w przegladarce. Prywatne
pliki runtime, np. `.agentic-os/state.db`, dalej nie moga byc serwowane.

### 2. Candidate review nie mial akcji bulk approval

CLI pozwala akceptowac kandydatow pojedynczo, ale dashboard wystawial tylko
przyciski per wiersz: Approve / Reject / Needs decision. Przy eksploracyjnym
testowaniu publicznej strony to doklada zbednej recznej pracy i sprawia, ze
dashboard jest slabszy niz CLI.

Bulk approval musi byc konserwatywne: powinno akceptowac tylko wykonywalne
kandydaty API/UI i pomijac manualne koszyki, np. security, accessibility,
juz odrzucone, juz zaakceptowane albo not-testable.

### 3. Wylaczona powierzchnia API byla ignorowana przez heurystyki

Dla strony online skonfigurowanej jako UI-only tekst typu `GET /rss` albo `GET
/sitemap` byl traktowany jako wejscie do kontraktu API mimo
`sut.api.enabled=false`. Planner mogl przez to generowac kandydatow API dla
serwisu, w ktorym operator jawnie wylaczyl testy API.

To tlumaczy zle kandydaty wywnioskowane z tekstu o publicznej stronie, zamiast
z realnej, skonfigurowanej powierzchni API.

### 4. Parsowanie URL-i moglo zamieniac domeny w falszywe trasy

Stare wyciaganie tras skanowalo surowy tekst pod slash-podobne tokeny. URL taki
jak `https://quality-blog.eu/` mogl zostawic fragment domeny w detekcji tras,
zamiast dac zamierzona trase homepage `/`.

Analyzer powinien najpierw parsowac prawdziwe URL-e, potem usuwac URL-e z
heurystycznego skanowania tras i nie traktowac tekstu domeny jako sciezki
aplikacji.

### 5. Skille pozwalaly na zbyt plytka eksploracje

Kilka skilli providerow pozwalalo interpretowac eksploracyjne testowanie
publicznej strony zbyt wasko. Dla publicznego serwisu jeden lub dwa smoke testy
to za malo, chyba ze task jawnie tak ogranicza zakres.

Warstwa skilli potrzebuje twardych instrukcji: odkrywanie tras/linkow,
sprawdzenie assetow, obserwacja bledow konsoli, reprezentatywne strony i review
fail, gdy pokrycie jest plytkie.

## Decyzja o katalogu runtime

Niedzialajace linki dashboardu wynikaly z allowlisty `/files`, nie z samego
katalogu z kropka. Pozniejszy pass kompatybilnosciowy przeniosl jednak
domyslny runtime root do widocznego `agentic-os-runtime/`, bo to ulatwia
sprawdzanie plikow, screenshoty i inspekcje operatorska. Legacy `.agentic-os/`
dalej dziala, gdy jest jawnie ustawione w configu albo jest jedynym istniejacym
katalogiem runtime.

## Wprowadzone zmiany

- Rozszerzono bezpieczne serwowanie plikow dashboardu o artefakty operatorskie:
  analysis, plans, task specs i run triage.
- Prywatny stan runtime dalej jest blokowany w `/files`, w tym runtime
  `state.db`.
- Dodano API dashboardu i kontrolke UI "Approve all runnable".
- Bulk approval pomija kandydatow niewykonywalnych albo manual-only.
- Analiza respektuje wylaczone powierzchnie API/web.
- Ekstrakcja tras najpierw parsuje URL-e, a dopiero potem stosuje heurystyki.
- UI-only taski nie dostaja juz nieistotnego ostrzezenia o braku OpenAPI.
- Planning nie dodaje pozycji z OpenAPI, gdy API jest wylaczone.
- Zaktualizowano wszystkie skille QualityCat dla Claude/Codex/Gemini, zeby
  wymagaly glebszej eksploracji publicznych stron i ostrzejszego review zbyt
  plytkiego pokrycia.

## Dalsze kroki

- Dodac browser-driven regresje dashboardu: utworzenie taska, wejscie w
  candidate review, klik "Approve all runnable" i sprawdzenie stanu tabeli.
- Dodac tryb crawlera/generatora tras dla publicznych stron, zeby szerokosc
  eksploracji wynikala z odkrytych same-origin linkow, nie tylko z tekstu taska.
- Ulepszyc edytor candidate table, zeby operator mogl bulk-approve z domyslnymi
  asercjami i potem poprawic pojedyncze wiersze przed generacja.
