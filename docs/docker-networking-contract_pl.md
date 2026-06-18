# Sieć Dockera i osiągalność zewnętrznego SUT

Status: active

Jak Agentic OS działający w kontenerze dociera do **zewnętrznego** SUT
(endpointy web/API + opcjonalna baza danych) i skąd biorą się sekrety SUT.
OS nigdy nie uruchamia SUT. Zobacz
`ADR-0001`
oraz wolumenową stronę kontraktu w
[`docs/docker-volume-contract_pl.md`](docker-volume-contract_pl.md).

Ten dokument definiuje **kontrakt**. *Okablowanie* w Compose, które go
egzekwuje — `extra_hosts`, przekazanie zmiennych środowiskowych, publikowany
port dashboardu — ląduje w `docker-compose.yml` (issue #353).

## Osiągalność

Wewnątrz kontenera `127.0.0.1` / `localhost` to **sam kontener**, nie host.
Cele osiągaj odpowiednio:

- **SUT na hoście** → użyj `host.docker.internal`.
  - macOS / Windows (Docker Desktop): `host.docker.internal` rozwiązuje się
    do hosta automatycznie.
  - Linux: Compose musi dodać `extra_hosts: ["host.docker.internal:host-gateway"]`,
    aby nazwa się rozwiązywała (okablowane w #353).
- **Zdalny / staging SUT** → publiczny URL wstrzyknięty przez env
  (`https://staging.example.com`). Rozwiązuje się przez zwykły DNS; bez
  mapowania hosta.

## Healthcheck wewnątrz kontenera

`sut.healthcheck.command` (np. `curl -fsS <url>`) działa **wewnątrz**
kontenera, więc jego URL musi być osiągalny z przestrzeni sieciowej kontenera
— SUT na hoście używa `host.docker.internal`, zdalny SUT używa swojej
publicznej nazwy hosta. Ta sama reguła dotyczy `sut.web.url` / `sut.api.url`,
które runtime wstrzykuje do runnera testów jako `UI_BASE_URL` /
`API_BASE_URL` (issue #92): też są wybierane z wnętrza kontenera.

`agentic-os doctor` waliduje, że `healthcheck.command` to niepusta lista
argv; samo sondowanie wykonywane jest w runtime.

## Opcjonalna referencja bazy danych

`sut.db` to blok **wyłącznie referencyjny** — nigdy wbudowany sekret:

```yaml
sut:
  db:
    ref_type: env     # env | file | none
    value: SUT_DB_PASSWORD # nazwa zmiennej env (lub, dla ref_type: file, ścieżka względna w repo)
```

Wartość, do której odnosi się referencja, trzyma pełny DSN
(`driver://user:pass@host:port/dbname`). Sterownik, host, port, użytkownik i
nazwa bazy żyją **wewnątrz** DSN, nie jako osobne klucze konfiguracji — to
trzyma szczegóły połączenia w jednym kontrolowanym przez operatora sekrecie i
unika rozlania sekretów w configu. Dla bazy na hoście hostem w DSN jest
`host.docker.internal`; dla bazy zdalnej — jej nazwa hosta.

OS nie wykonuje dziś żadnego połączenia z bazą; blok jest zadeklarowaną
referencją rozwiązywaną w runtime przez to, co ją konsumuje (skrypty
operatora / wygenerowane testy). Wbudowany check bazy w kontenerze miałby
własną konsumpcję tej referencji.

## Sekrety — nic wbudowane, tylko referencje, redakcja

- **Nic wbudowanego w obraz.** `Dockerfile` dostarcza tylko przykładowy
  config i pliki wspierające; żywy `config/agentic-os.yml` oraz wszystkie
  sekrety podawane są w runtime przez środowisko, zamontowany config lub
  zamontowane pliki sekretów (#353 okablowuje przekazanie).
- **Runner testów SUT nigdy nie dostaje poświadczeń modeli operatora.**
  `scrub_provider_credentials()` usuwa `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `CLAUDE_CODE_OAUTH_TOKEN`, `GEMINI_API_KEY`, … ze środowiska dziecka, zanim
  zobaczy je runner dostarczony przez SUT (issue #291).
- **Tylko referencje — bez wbudowanych sekretów.** Walidator configu odrzuca
  `sut.db` / `sut.credentials`, których `ref_type` nie jest `env` / `file` /
  `none`; wbudowany DSN/token nie przechodzi walidacji (kod wyjścia 2).
- **Redakcja logów.** Linie stdout/stderr/status runnera przechodzą przez
  `redact_sensitive_text()`, które usuwa:
  1. **wartości** zmiennych env, których **nazwa** pasuje do słowa-klucza
     sekretu (`api_key`, `apikey`, `secret`, `password`, `passwd`, `token`,
     `bearer`, `credential`, `access_key`, `private_key`, `client_secret`);
  2. literały o kształcie sekretu — nagłówki `Authorization:`, `bearer …`,
     `sk-…`, `ghp_…`/`ghs_…`, klucze AWS `AKIA…`, bloki kluczy prywatnych PEM,
     `password=…`.

  Niezależnie, `env_hash()` (odcisk dry-run) przepuszcza całe środowisko przez
  SHA-256; przechowuje tylko skrót, nigdy surowe wartości.

### Ograniczenie redakcji — nazywaj zmienne sekretów słowem-kluczem

Redakcja wartości env opiera się na **nazwie** zmiennej. DSN trzymany w
zmiennej nazwanej `DATABASE_URL` (bez słowa-klucza w nazwie) **nie** jest
automatycznie redagowany, jeśli pojawi się w wyjściu runnera. Dopóki redakcja
nie honoruje zadeklarowanych w configu referencji sekretów (follow-up
**#385**), nazywaj zmienne niosące sekrety rozpoznawanym słowem-kluczem, aby
były objęte — np. `SUT_DB_PASSWORD`, `SUT_DB_CREDENTIAL`, `*_SECRET`,
`*_TOKEN`. Przykład `credentials` używa `TEST_USER_TOKEN` (zawiera „token"),
co **jest** objęte dziś.

## Przykłady z życia

### (a) SUT na hoście

```yaml
sut:
  root: .
  mode: online
  healthcheck:
    command: ["curl", "-fsS", "http://host.docker.internal:3000/health"]
    timeout_seconds: 30
    retries: 10
  test_runner: ./run-tests.sh
  web:
    enabled: true
    url: http://host.docker.internal:3000
  api:
    enabled: true
    url: http://host.docker.internal:3000/api
```

Na Linuksie Compose dodaje `extra_hosts: ["host.docker.internal:host-gateway"]`
(#353); na Docker Desktop już się rozwiązuje.

### (b) SUT na URL staging

```yaml
sut:
  root: .
  mode: online
  healthcheck:
    command: ["curl", "-fsS", "https://staging.example.com/health"]
    timeout_seconds: 30
    retries: 10
  test_runner: ./run-tests.sh
  web:
    enabled: true
    url: https://staging.example.com
  api:
    enabled: true
    url: https://staging.example.com/api
```

### (c) SUT + baza danych

```yaml
sut:
  root: .
  mode: online
  healthcheck:
    command: ["curl", "-fsS", "http://host.docker.internal:3000/health"]
    timeout_seconds: 30
    retries: 10
  test_runner: ./run-tests.sh
  web:
    enabled: true
    url: http://host.docker.internal:3000
  db:
    ref_type: env
    value: SUT_DB_PASSWORD   # nazwa zmiennej env; trzyma sekret/DSN. Nazwana słowem-kluczem, by logi ją redagowały.
```

Sekret podawaj w runtime przez przekazanie env z Compose (#353), np.
`SUT_DB_PASSWORD=postgres://user:pass@host.docker.internal:5432/app`. Dla bazy
zdalnej użyj jej nazwy hosta zamiast `host.docker.internal`.
