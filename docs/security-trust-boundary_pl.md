# Granica zaufania bezpieczeństwa

Status: aktywny. Obejmuje lokalny serwer dashboardu, serwowanie plików
runtime oraz uruchamianie podprocesów SUT. Napisany dla issue #291 (runda 2
hardeningu); mechanizmy rundy 1 (`security.py`, guard host-rebinding) są
zakładane jako obecne.

Dashboard Agentic OS to narzędzie **wyłącznie loopback, jednooperatorowe**.
Każda kontrola poniżej jest ograniczona do tego modelu. Żadna z nich nie czyni
dashboardu bezpiecznym do wystawienia w sieci współdzielonej — trzymaj go
związanym z `127.0.0.1`.

## 1. Uwierzytelnianie metod modyfikujących dashboardu

`do_POST` / `do_PUT` / `do_DELETE` przechodzą przez `_enforce_unsafe_request`,
który stosuje trzy sprawdzenia w kolejności:

1. **Nagłówek Host** musi wskazywać hosta loopback (obrona przed DNS-rebinding,
   issue #148).
2. **Origin / Referer** musi być loopback lub nieobecny (obrona CSRF, issue #148).
3. **`X-Agentic-Token`** musi równać się tokenowi serwera (issue #291),
   porównanie przez `hmac.compare_digest`.

### Cykl życia tokenu

- Rozwiązywany raz na serwer w `_load_or_create_dashboard_token`:
  zmienna `AGENTIC_DASHBOARD_TOKEN` → istniejący `<runtime_root>/.dashboard_token`
  (tryb `0600`) → świeży `secrets.token_urlsafe(32)` zapisany z `0600`.
- Serwer osadza token w każdej renderowanej stronie HTML (tag
  `<meta name="agentic-dashboard-token">` plus jednolinijkowy shim
  `window.fetch`, który dołącza nagłówek do same-origin POST/PUT/DELETE).

### Co token broni, a czego nie

| Zagrożenie | Bronione? | Czym |
|---|---|---|
| Cross-origin strona przeglądarki POST-ująca na localhost (CSRF) | Tak | Guard Origin **oraz** token (atakująca strona nie może odczytać ciała odpowiedzi cross-origin, więc nie pozna tokenu) |
| Proces innego **użytkownika OS** | Tak | Plik tokenu `0600` jest nieczytelny dla innych użytkowników |
| Proces **tego samego użytkownika OS**, który może `cat .dashboard_token` | **Nie** | To granica konta użytkownika OS; poza zakresem lokalnego narzędzia jednooperatorowego |
| Atakujący sieciowy | N/D | Serwer wiąże tylko loopback |

Jeśli pliku tokenu nie da się zapisać (read-only katalog runtime), token w
pamięci nadal bramkuje działający proces; tracona jest tylko trwałość.

`enable_write_endpoints` to flaga **funkcji** (które endpointy istnieją), nie
flaga **tożsamości**. Token jest sprawdzeniem tożsamości i jest zawsze
wymuszany, gdy token jest udostępniony.

## 2. Serwowanie plików runtime (`/files/`)

`_serve_runtime_file` serwuje artefakty tylko do odczytu i jest jedyną ścieżką
mapującą URL na system plików. Hardening (issue #291):

- Sufiks przechodzi przez `security.resolve_repo_path`, które odrzuca bajty
  NUL, ścieżki absolutne, rozwijanie `~` oraz ucieczki `..`, i gwarantuje, że
  rozwiązany cel pozostaje pod `repo_root`.
- Cel musi dodatkowo mieścić się w jednej **jawnej liście dozwolonych**
  serwowanych katalogów (reports, bugs, analysis, plans, task specs, runs,
  evidence, patches, logi podprocesów, support bundles). Każdy wpis listy jest
  `.resolve()`-owany, więc sprawdzenie zawierania porównuje w pełni rozwiązane
  ścieżki po obu stronach — dowiązany symbolicznie katalog nie przemyci ścieżki.
- Cokolwiek nie przejdzie któregokolwiek sprawdzenia, zwraca `404` (nigdy
  `403`), więc trasa nie potwierdza istnienia ścieżek poza listą dozwolonych.

Ładunki regresyjne (`../`, `reports/../../secret`, `/etc/passwd`,
`%2Fetc%2Fpasswd`, ucieczka symlinkiem, ścieżka repo spoza listy) są pokryte w
`tests/test_dashboard_server.py`.

## 3. Granica zaufania podprocesów SUT

Komendy dostarczone przez SUT (`sut.healthcheck.command`, `sut.test_runner`,
compose up/down, runner baseline eksploracyjnego) to **niezaufane własne
binaria**. Działają na maszynie operatora, bez sandboxa, z uprawnieniami
plikowymi operatora. Agentic OS ich nie konteneryzuje; granica zaufania to:

- **Tylko argv.** Każda komenda jest walidowana przez `security.require_safe_argv`
  (bez stringów shella, bez `sh -c`, bez bajtów NUL) i rozwiązywana względem
  wyselekcjonowanej PATH. To pozostaje bez zmian i nie wolno tego osłabiać.
- **Bez poświadczeń modeli.** Klucze API dostawców (`ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, …) są wydzielone z dziedziczonego env. Komendy SUT startują
  z `include_provider_credentials=False`, a jakikolwiek jawny env przekazany
  komendzie SUT (np. kopia `os.environ` test_runnera) jest najpierw przepuszczany
  przez `scrub_provider_credentials`. Wrogi binarka SUT nie może więc odczytać
  kluczy modeli operatora ze swojego środowiska.
- **Tylko core env.** Dzieci SUT nadal otrzymują `PATH`, `HOME`, `LANG`,
  `LC_ALL`, `TZ`, `TMPDIR`, aby legalne runnery (w tym Playwright) działały.

Wywołania CLI modeli zachowują `include_provider_credentials=True` (domyślnie),
ponieważ te CLI potrzebują kluczy do uwierzytelnienia.

Co pozostaje odpowiedzialnością operatora: kieruj komendy `sut.*` wyłącznie na
binaria, którym ufasz, ponieważ wykonują się z uprawnieniami Twojego konta.

## 4. Integralność odpowiedzi (odroczone)

Odpowiedzi są niepodpisane i tylko HTTP na loopback. Podpisywanie lub TLS są
**poza zakresem**, dopóki serwer jest tylko loopback (issue #291 zaznacza to
jako opcjonalne). Wrócić tylko, jeśli kiedykolwiek wprowadzone zostanie
wystawienie dashboardu poza loopback.
