# Analiza dashboardu i generowania taskow z dokumentow

Status: superseded
Superseded-on: 2026-05-22
Superseded-by: operator-guide_pl.md (sekcja "Wciąganie zewnętrznych dokumentów taskowych")
Reason: zapis uzasadnienia projektu inbox/pretask synthesis. Opisane decyzje
(kanoniczny `inbox/`, alias `pretask/`, `inbox synthesize`, mirror write-gate
w dashboardzie, link-back wyników) trafiły na `main` (PR-y #133, #171).
Traktuj jako historyczne uzasadnienie; aktualny przepływ — `operator-guide_pl.md`.

English canonical: [`dashboard-inbox-task-generation.md`](dashboard-inbox-task-generation.md).

## Zakres

Ten pass sprawdzil sciezke operatorska dashboardu po migracji runtime root do
widocznego katalogu, ze szczegolnym naciskiem na zamiane wrzuconej dokumentacji
w sensowne task-specs z CLI i dashboardu.

## Ustalenia

1. Dashboard mial juz upload + ingest per plik, ale operator nadal musial
   wybierac miedzy recznym formularzem i jednym taskiem na jeden dokument.
   Realny intake czesto zaczyna sie jako paczka: notatki feature, ograniczenia
   QA, URL publicznej strony, znane bugi i acceptance criteria. Stary przeplyw
   nie skladal tego w jeden spojny task.
2. Disk intake istnial tylko jako `inbox/`. Operator pytal o widoczny folder
   pre-task; alias `pretask/` daje jasniejsze miejsce stagingowe bez drugiego
   modelu runtime.
3. Kafelek inbox nie odzwierciedlal write gate przed kliknieciem. Przyciski
   upload/ingest mogly wygladac na aktywne przy wylaczonych zapisach, a potem
   konczyc sie odpowiedzia 403.
4. Wyniki ingest byly tylko tekstowe. Operator widzial ID utworzonego taska,
   ale nie mial linku do przejscia dalej w dashboardzie.
5. `inbox/README.md` nadal wskazywal stary ukryty runtime path.

## Decyzje wdrozone

- `inbox/` zostaje kanonicznym katalogiem intake.
- Dodano `pretask/` jako sledzony, widoczny alias stagingowy dla paczek.
- `inbox ingest` zostaje trybem jeden plik = jeden task.
- Dodano `inbox synthesize [--title ...]`, ktory tworzy jeden task z wszystkich
  pending dokumentow w `inbox/` i `pretask/`.
- Ten sam tryb jest w dashboardzie jako **Create task from pending**.
- Spec jest generowany deterministycznie, nie jako model-only output. Dzieki
  temu CLI/dashboard sa skryptowalne i dzialaja jeszcze przed wywolaniem modelu.
- Downstream quality gates zostaja bez zmian: synthesized spec musi przejsc
  analyze, plan, candidate review i jawne approval zanim powstana wykonywalne
  testy.

## Zawartosc wygenerowanego taska

Wygenerowany task-spec zawiera:

- liste dokumentow zrodlowych ze sciezkami wzglednymi;
- wyciagniete linie wymagan;
- wykryte endpointy API, URL-e i sciezki stron;
- sygnaly znanych bugow;
- ograniczenia danych testowych, credentials i cleanup;
- pytania otwarte, gdy brakuje powierzchni lub credentials;
- ostrzezenie, ze kandydaci nadal musza miec dokladna asercje, dane i cleanup
  przed generowaniem testow.

## Wplyw na dashboard

- `/api/inbox` zwraca katalog kanoniczny i wspierane katalogi intake.
- `/api/inbox/synthesize` tworzy jeden task z pending bundle.
- `/tasks/new` oferuje Upload, Ingest pending i Create task from pending.
- Przyciski inbox respektuja efektywny write gate (`enable_write_endpoints`,
  `serve --full` albo write unlock z full autonomy).
- Wyniki z utworzonym taskiem sa linkami do `/tasks/<id>`.

## Follow-upy

- Dodac browser-driven regresje dla `/tasks/new`, gdy harness browserowy bedzie
  dostepny w CI: upload pliku, synthesize, przejscie linkiem do taska,
  analyze/plan.
- Rozwazyc opcjonalny OCR dla skanowanych PDF-ow, jesli operator zacznie
  uzywac obrazkowych PDF-ow. Obecne wsparcie PDF celowo wymaga tekstu
  wyciagalnego przez `pypdf`.
