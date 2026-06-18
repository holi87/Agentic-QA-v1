# Coding Standards — Java BDD Stack (Cucumber + Playwright + RestAssured)

Status: superseded
Superseded-on: 2026-05-29
Superseded-by: ADR-0002
Reason: kanonicznym stackiem generowanych testów jest Playwright + TypeScript (poniższy stack Java BDD był odziedziczony z poprzedniego projektu). Standardy kodowania PW+TS śledzone w #366.

> **Superseded.** Ten dokument opisuje **odziedziczony** stack Java BDD.
> Kanonicznym stackiem generowanych testów jest teraz **Playwright +
> TypeScript** — patrz
> `ADR-0002`. Trzymany
> dla kontekstu historycznego do czasu wprowadzenia standardów Playwright + TS
> (#366); nie traktuj go jako obecnego kontraktu.

Ten plik = JAK piszemy kod. Siostrzany dokument `qa-standards.md` = JAKI jest próg jakości (ISO 25010, ISO/IEC/IEEE 29119, ISTQB, OWASP API Top 10, WCAG 2.2). Zawsze czytaj oba. coding-standards przepisuje strukturę, nazwy, idiomy; qa-standards przepisuje cele pokrycia, severity, traceability. Gdy konflikt — qa-standards wygrywa w kwestii intencji, coding-standards wygrywa w kwestii składni.

Założony stack: Java 17 LTS, Gradle 8.x Kotlin DSL, JUnit 5 Jupiter, Cucumber JVM 7.x + Picocontainer + cucumber-junit-platform-engine, RestAssured 5.x, Playwright for Java, AssertJ + SoftAssertions, Allure 2.x.

---

## 1. SOLID i Clean Code

Reguły. Nienegocjowalne w hackathonie 8h — oszczędzają czas, nie kosztują.

- **SRP per klasa step def.** Jeden obszar feature = jedna klasa step def. Auth steps != Order steps.
- **DI via Picocontainer.** Bez `static`, bez singletonów, bez `ThreadLocal` na stan scenariusza. Tylko constructor injection.
- **DIP.** Steps zależą od abstrakcji (`UserApiClient`, `LoginPage`), nie bezpośrednio od `RequestSpecification` ani `Page`.
- **Brak logiki biznesowej w steps.** Step = parsuj Gherkin + deleguj. Logika żyje w clients/page objects.
- **Brak współdzielonego mutowalnego stanu static.** Równoległe scenariusze zjedzą cię żywcem.

```java
// GOOD — SRP, constructor injection, no static
package com.example.steps;

import com.example.api.UserApiClient;
import com.example.context.ScenarioContext;
import io.cucumber.java.en.When;

public final class UserSteps {
    private final UserApiClient userApi;
    private final ScenarioContext ctx;

    public UserSteps(UserApiClient userApi, ScenarioContext ctx) { // Pico injects both
        this.userApi = userApi;
        this.ctx = ctx;
    }

    @When("admin creates user {string}")
    public void adminCreatesUser(final String email) {
        ctx.setLastResponse(userApi.create(email));
    }
}
```

```java
// BAD — static state, business logic in step, no DI
public class UserSteps {
    static String lastUserId; // race condition with parallel scenarios
    @When("admin creates user {string}")
    public void create(String email) {
        var resp = RestAssured.given().baseUri("http://...").post("/users"); // hardcoded, no client
        if (resp.statusCode() == 200) lastUserId = resp.path("id"); // logic in step
    }
}
```

Źródła: `/cucumber/cucumber-jvm` (PicoContainer step injection), Robert C. Martin — Clean Code Ch.3 (Functions), Ch.10 (Classes).

---

## 2. Struktura projektu

Layout modułu Gradle z **jawnym podziałem framework / testy**. Część framework żyje w
`src/main/java` i jest własnością frameworka (zmiany przechodzą przez gate'y `unitTest`). Część
testów projektu żyje w `src/test/java` i jest swobodnie wymienialna per projekt.

```
qualitycat/
├── build.gradle.kts
├── settings.gradle.kts
├── gradle/
│   └── libs.versions.toml          # version catalog
├── src/
│   ├── main/                       # FRAMEWORK INFRASTRUCTURE (owned)
│   │   └── java/pl/qualitycat/
│   │       ├── support/            # Config, World, Hooks, AssertionHook
│   │       ├── ui/                 # PlaywrightContext (lifecycle, screenshot-on-fail)
│   │       ├── api/client/         # ApiClient — base RestAssured spec factory
│   │       └── framework/
│   │           ├── assertions/     # HttpAsserts (status/CT/SLA/error-shape/header-leak)
│   │           └── json/           # JsonSchemas (schema validation helper)
│   ├── test/                       # PROJECT TESTS + REFERENCE (replaceable)
│   │   ├── java/pl/qualitycat/
│   │   │   ├── api/clients/        # <Area>ApiClient.java — uses ApiClient.requestSpec()
│   │   │   ├── api/models/         # records / DTOs
│   │   │   ├── api/builders/       # Faker-driven test data builders
│   │   │   ├── api/steps/          # <Area>ApiSteps.java
│   │   │   ├── ui/pages/           # <Area>Page.java (Playwright POs)
│   │   │   ├── ui/steps/           # <Area>UiSteps.java
│   │   │   └── runners/            # JUnit Platform Suite per tag bucket
│   │   └── resources/
│   │       ├── features/           # *.feature, kebab-case (active suite)
│   │       ├── _reference/         # users.feature + login.feature (NOT runner-scanned)
│   │       ├── schemas/            # JSON schemas consumed by JsonSchemas
│   │       ├── allure.properties
│   │       ├── cucumber.properties
│   │       ├── junit-platform.properties
│   │       └── logback-test.xml
│   └── unitTest/                   # FRAMEWORK UNIT TESTS (no Cucumber)
│       └── java/pl/qualitycat/framework/
│           ├── ConfigTest.java
│           ├── WorldTest.java
│           ├── HttpAssertsTest.java
│           └── JsonSchemasTest.java
└── README.md
```

Dlaczego ten layout:

- **Glue Cucumbera nadal działa.** Source set `test` w Gradle domyślnie ma `main` na classpath,
  więc `Hooks`, `AssertionHook` i `PlaywrightContext` są auto-discovered, gdy runner skanuje
  pakiet glue `pl.qualitycat`.
- **Zmiany frameworka mają własny gate.** `./gradlew unitTest` waliduje klasy frameworka
  w izolacji. Testy projektu mogą się zepsuć bez unieważnienia poprawności frameworka.
- **Testy projektu zostają skupione.** `src/test/java` zawiera tylko kod specyficzny dla projektu.
  Reviewerzy i ludzie bez kontekstu nawigują tam w sekundach.
- **Future-proof.** Część framework można wyekstrahować do opublikowanego artefaktu bez
  przekładania plików — ścieżki pakietów i widoczność są już poprawne.

Źródła: `/cucumber/cucumber-jvm` (junit-platform-engine layout), `/gradle/gradle` userguide — Java testing project layout.

---

## 3. Konwencje nazewnicze

Wybierz jedną regułę, stosuj wszędzie. Reviewer nigdy nie powinien się zastanawiać.

| Artefakt | Konwencja | Przykład |
|---|---|---|
| Klasa step def | `<Domain>Steps` | `UserSteps.java`, `LoginSteps.java` |
| Page object | `<Page>Page` | `LoginPage.java`, `DashboardPage.java` |
| API client | `<Resource>ApiClient` | `UserApiClient.java`, `OrderApiClient.java` |
| Klasa Hooks | `Hooks` lub `<Domain>Hooks` | `Hooks.java`, `DbHooks.java` |
| Runner | `Run<Bucket>Test` | `RunSmokeTest.java`, `RunRegressionTest.java` |
| Plik feature | kebab-case rzeczowniki | `user-creation.feature`, `login-negative.feature` |
| Scenariusz | tryb rozkazujący, język biznesowy | `Scenario: Admin creates active user` |
| Metoda step | `verbObject` camelCase | `adminCreatesUser`, `userShouldBeActive` |
| Test data builder | `<Entity>Builder` | `UserBuilder.java` |

Metody step NIE muszą lustrzanie odzwierciedlać Gherkina słowo w słowo — Cucumber bind'uje przez regex w adnotacji, nie przez nazwę metody. Więc nazwy metod rób czytelne kodowo.

```java
@When("admin creates user {string} with role {string}")
public void adminCreatesUserWithRole(final String email, final String role) { ... }
```

Źródła: Google Java Style Guide §5 (Naming), `/cucumber/docs` Gherkin reference.

---

## 4. Standardy Cucumber BDD

Gherkin po **angielsku**. Język biznesowy. Bez "click button". Bez przecieku technicznego.

Reguły:

- **Feature** = jedna zdolność. Jeden plik. ≤ 10 scenariuszy.
- **Background** = warunki wstępne wspólne dla WSZYSTKICH scenariuszy w pliku. Nie "rzeczy, których mi się nie chce powtarzać".
- **Scenario Outline**, gdy ten sam flow z różnymi danymi; **Data Table**, gdy pojedynczy scenariusz przyjmuje strukturalny input.
- **Jeden runner per tag bucket** — `RunSmokeTest`, `RunCriticalTest`, `RunRegressionTest`. Komponuj przez `cucumber.filter.tags`.
- Reuse stepów: napisz 100 krótkich stepów, zanim 10 god-stepów.

Taksonomia tagów (obowiązkowa — qa-standards Sec. tagging):

| Tag | Znaczenie |
|---|---|
| `@smoke` | 3-5 testów, SUT żyje, < 30s |
| `@critical` | rdzeniowa logika biznesowa |
| `@regression` | każdy test (pełny suite) |
| `@negative` | 4xx/5xx, niepoprawny input |
| `@boundary` | wartości brzegowe (0, MAX_INT, empty string) |
| `@security` | OWASP top 10 (IDOR, injection, mass assignment) |
| `@functional-<area>` | per feature/domena (`@functional-users`) |
| `@extended` | parametryzacja, głębsze warianty |

```gherkin
# src/test/resources/features/api/user-creation.feature
@functional-users @regression
Feature: User creation via Admin API

  Background:
    Given an admin token is obtained

  @smoke @critical
  Scenario: Admin creates active user with valid email
    When admin creates user "alice@example.com" with role "USER"
    Then response status is 201
    And user "alice@example.com" exists with role "USER"

  @negative @boundary
  Scenario Outline: Admin cannot create user with invalid email
    When admin creates user "<email>" with role "USER"
    Then response status is 400
    And error code is "EMAIL_INVALID"
    Examples:
      | email                |
      |                      |
      | not-an-email         |
      | a@                   |
      | @b.com               |

  @security
  Scenario: Non-admin cannot create user (authz)
    Given a user token is obtained
    When current actor creates user "x@example.com" with role "USER"
    Then response status is 403
```

Data table vs Outline — tabela, gdy scenariusz przyjmuje jeden strukturalny zbiór inputu; outline, gdy ten sam scenariusz wykonuje się ponownie z różnymi wierszami.

```gherkin
@functional-orders
Scenario: Place order with multiple items
  Given the cart contains:
    | sku    | qty | price |
    | SKU-1  | 2   | 9.99  |
    | SKU-2  | 1   | 19.99 |
  When the buyer submits the order
  Then the order total is 39.97
```

Źródła: `/cucumber/docs` (Gherkin reference), `/cucumber/cucumber-jvm` (junit-platform suite filter tags).

---

## 5. Step Definitions

Stepy są cienkie. Każdy step:

1. Parsuje argumenty z Gherkina.
2. Wywołuje client/PO.
3. Utrwala wynik w `ScenarioContext`.

Bez `if`, bez pętli wokół logiki asercji, bez `try/catch` połykającego. Hooki tylko do setup/teardown — nigdy do asercji.

```java
// ScenarioContext shared across step classes via Picocontainer
package com.example.context;

import io.restassured.response.Response;

public final class ScenarioContext {
    private Response lastResponse;
    private String currentUserId;

    public Response getLastResponse() { return lastResponse; }
    public void setLastResponse(final Response r) { this.lastResponse = r; }
    public String getCurrentUserId() { return currentUserId; }
    public void setCurrentUserId(final String id) { this.currentUserId = id; }
}
```

```java
// Hooks with Pico injection — Scenario param for Allure attachments
package com.example.hooks;

import com.example.context.ScenarioContext;
import com.example.ui.BrowserManager;
import io.cucumber.java.After;
import io.cucumber.java.Before;
import io.cucumber.java.Scenario;

public final class Hooks {
    private final BrowserManager browser;
    private final ScenarioContext ctx;

    public Hooks(BrowserManager browser, ScenarioContext ctx) {
        this.browser = browser;
        this.ctx = ctx;
    }

    @Before("@ui")
    public void openBrowser() { browser.start(); }

    @After("@ui")
    public void closeBrowser(Scenario scenario) {
        if (scenario.isFailed() && browser.page() != null) {
            byte[] png = browser.page().screenshot();
            scenario.attach(png, "image/png", "failure-screenshot");
        }
        browser.stop();
    }
}
```

Źródła: `/cucumber/cucumber-jvm` — Hooks API, PicoContainer step DI.

---

## 6. Page Object Model — Playwright Java

Strategia lokatorów — accessibility-first, nigdy łańcuchy klas CSS.

Priorytet: `getByRole` > `getByLabel` > `getByPlaceholder` > `getByText` > `getByTestId` > CSS.

Reguły:
- Jeden PO na stronę (lub istotny komponent).
- Konstruktor przyjmuje `Page` (Playwright page).
- **Brak `Thread.sleep`.** Playwright auto-waits — używaj `assertThat(locator).isVisible()` / `.waitFor()`.
- PO ujawniają akcje i query. Asercje żyją w stepach (nie w PO), chyba że asercja web-first na poziomie komponentu.
- Pola `Locator` są `final`. Brak mutacji po konstrukcji.

```java
package com.example.ui;

import com.microsoft.playwright.Locator;
import com.microsoft.playwright.Page;
import com.microsoft.playwright.options.AriaRole;

import static com.microsoft.playwright.assertions.PlaywrightAssertions.assertThat;

public final class LoginPage {
    private final Page page;
    private final Locator emailInput;
    private final Locator passwordInput;
    private final Locator submitButton;
    private final Locator errorBanner;

    public LoginPage(final Page page) {
        this.page = page;
        this.emailInput = page.getByLabel("Email");
        this.passwordInput = page.getByLabel("Password");
        this.submitButton = page.getByRole(AriaRole.BUTTON,
                new Page.GetByRoleOptions().setName("Sign in"));
        this.errorBanner = page.getByTestId("login-error");
    }

    public LoginPage open(final String baseUrl) {
        page.navigate(baseUrl + "/login");
        assertThat(submitButton).isVisible();   // auto-wait, no sleep
        return this;
    }

    public void loginAs(final String email, final String password) {
        emailInput.fill(email);
        passwordInput.fill(password);
        submitButton.click();
    }

    public String errorText() {
        assertThat(errorBanner).isVisible();
        return errorBanner.textContent();
    }
}
```

Źródła: `/microsoft/playwright-java` (locators, PlaywrightAssertions auto-wait), `/websites/playwright_dev` (Best Practices — locators).

---

## 7. Warstwa API — RestAssured

Nigdy nie składaj URL-i przez konkatenację stringów. Nigdy nie wpisuj base URI w testach. Jeden `RequestSpecBuilder` + `ResponseSpecBuilder` współdzielony per klient.

Reguły:
- Logging: `LogDetail.IF_VALIDATION_FAILS` tylko — cisza na zielonym, pełny dump na czerwonym.
- Konfiguracja z env / properties — nigdy hardcoded host.
- Jedna klasa client per resource (`UserApiClient`, `OrderApiClient`).
- Zwracaj typowane DTO lub `Response` — niech step decyduje.

```java
package com.example.api;

import io.restassured.builder.RequestSpecBuilder;
import io.restassured.builder.ResponseSpecBuilder;
import io.restassured.filter.log.LogDetail;
import io.restassured.http.ContentType;
import io.restassured.response.Response;
import io.restassured.specification.RequestSpecification;
import io.restassured.specification.ResponseSpecification;

import static io.restassured.RestAssured.given;

public final class UserApiClient {
    private final RequestSpecification reqSpec;
    private final ResponseSpecification okSpec;

    public UserApiClient(final EnvConfig env, final TokenProvider tokens) {
        this.reqSpec = new RequestSpecBuilder()
                .setBaseUri(env.baseUrl())
                .setBasePath("/api/v1")
                .setContentType(ContentType.JSON)
                .addHeader("Authorization", "Bearer " + tokens.adminToken())
                .log(LogDetail.URI)                       // minimal noise on green
                .build();
        this.okSpec = new ResponseSpecBuilder()
                .expectContentType(ContentType.JSON)
                .build();
        // global toggle: only dump full req/resp when assertion fails
        io.restassured.RestAssured.enableLoggingOfRequestAndResponseIfValidationFails(
                LogDetail.ALL);
    }

    public Response create(final String email, final String role) {
        return given().spec(reqSpec)
                .body(new CreateUserRequest(email, role))
                .when().post("/users")
                .then().spec(okSpec)
                .extract().response();
    }

    public Response getById(final String id) {
        return given().spec(reqSpec)
                .pathParam("id", id)
                .when().get("/users/{id}")
                .then().extract().response();
    }
}
```

`record` dla request/response body — immutable, bez Lomboka:

```java
public record CreateUserRequest(String email, String role) {}
public record UserDto(String id, String email, String role, boolean active) {}
```

Źródła: [RestAssured Wiki — Usage](https://github.com/rest-assured/rest-assured/wiki/usage), [RestAssured RequestSpecBuilder source](https://github.com/rest-assured/rest-assured/blob/master/rest-assured/src/main/java/io/restassured/builder/RequestSpecBuilder.java), [REST-assured Best Practices Guide](https://github.com/merridewberry/rest-assured-best-practices).

---

## 8. Asercje — AssertJ + SoftAssertions

Dwie klasy asercji. Nigdy nie mieszaj.

- **BIZ (business)** — czego wymaga user/kontrakt. Status code jest poprawny, zwrócony `email` zgadza się z inputem, `active=true`. Opis Allure zaczyna się od `BIZ:`.
- **TECH (technical)** — schemat poprawny, response time < SLO, header `Content-Type` poprawny. Opis Allure zaczyna się od `TECH:`.

Używaj `SoftAssertions` per krok scenariusza, który asertuje > 1 rzecz → przechwytuje wszystkie niepowodzenia, nie tylko pierwsze.

```java
package com.example.steps;

import com.example.context.ScenarioContext;
import io.cucumber.java.en.Then;
import io.restassured.response.Response;
import org.assertj.core.api.SoftAssertions;

public final class UserAssertionSteps {
    private final ScenarioContext ctx;

    public UserAssertionSteps(ScenarioContext ctx) { this.ctx = ctx; }

    @Then("the created user matches input {string} with role {string}")
    public void verifyCreatedUser(final String expectedEmail, final String expectedRole) {
        final Response r = ctx.getLastResponse();
        final SoftAssertions soft = new SoftAssertions();

        // TECH: contract integrity
        soft.assertThat(r.statusCode())
            .as("TECH: HTTP status must be 201 Created")
            .isEqualTo(201);
        soft.assertThat(r.getContentType())
            .as("TECH: response Content-Type must be JSON")
            .contains("application/json");

        // BIZ: domain correctness
        soft.assertThat(r.jsonPath().getString("email"))
            .as("BIZ: returned email must equal request email")
            .isEqualTo(expectedEmail);
        soft.assertThat(r.jsonPath().getString("role"))
            .as("BIZ: role assignment must persist")
            .isEqualTo(expectedRole);
        soft.assertThat(r.jsonPath().getBoolean("active"))
            .as("BIZ: new user must be active by default per spec §3.2")
            .isTrue();

        soft.assertAll();
    }
}
```

Opis `as("BIZ: ...")` ląduje w nagłówku Allure failure — auditor go czyta. Warte tych klawiszy.

Źródła: `/assertj/assertj` (SoftAssertions, describedAs), Allure cucumber docs.

---

## 9. Bug-Aware Testing (CRITICAL)

**Asercje wyrażają SPECYFIKACJĘ, nie aktualne zachowanie SUT.** Nigdy nie pisz `.isEqualTo(actualWeirdValue)`, ponieważ `.isEqualTo(specValue)` jest czerwone. To dostosowywanie testu, by zazielenić = ukrywanie bugów. W QualityCat interesariusze sadzą bugi celowo. Wykrywanie > pass rate.

Workflow gdy czerwone:

1. Przeczytaj ponownie spec / API doc / OpenAPI.
2. Jeśli zachowanie SUT jest sprzeczne ze spec → **bug**. Zachowaj asercję poprawną. Otaguj scenariusz `@known-bug @bug-NNN` lub przenieś test do `@skip` TYLKO z powodem. Utwórz `bugs/BUG-NNN-<slug>.md` przez `/QC-claude-report-bug`.
3. Jeśli spec niejednoznaczny → oznacz `@ambiguous`, zanotuj jako `severity: Info` w `bugs/BUG-NNN-<slug>.md`, przyjmij najściślejszą rozsądną interpretację.
4. Jeśli test zły (zły endpoint, zły payload) → napraw test.

Nigdy po cichu nie odwracaj asercji. Nigdy `// TODO fix later` na czerwonym teście.

```java
// GOOD — assertion mirrors spec, fails because SUT bug
soft.assertThat(r.jsonPath().getBoolean("active"))
    .as("BIZ: §3.2 — new user MUST be active=true; SUT returns false → BUG-007")
    .isTrue();
```

```java
// BAD — adjusted to current (buggy) SUT
soft.assertThat(r.jsonPath().getBoolean("active"))
    .isFalse(); // why? hides BUG-007
```

Szablon wpisu `bugs/BUG-NNN-<slug>.md` (wg schematu `bug-reporting.md`):

```markdown
---
id: BUG-007
title: New user defaults to active=false
severity: High
likelihood: High
component: API / Logic
owasp: N/A
iso25010: Functional Suitability / Correctness
status: OPEN
opened_at: 2026-05-11T10:30:00Z
---

# BUG-007: New user defaults to active=false

## Steps to Reproduce
1. POST /users with valid payload (see evidence/BUG-007/request.http).

## Expected (per spec)
§3.2 — "new users MUST be created with active=true"

## Actual
POST /users returns `active=false`.

## Evidence
- features/api/user-creation.feature:12 (scenario kept failing, tagged `@known-bug @bug-007`)
- evidence/BUG-007/response.json
```

Źródła: ISTQB FL §1.4 (defects vs failures), qa-standards.md.

---

## 10. Integracja Allure

Zależność: `io.qameta.allure:allure-cucumber7-jvm`. Użyj `cucumber-junit-platform-engine`, podłącz Allure przez `META-INF/services` (auto) lub `cucumber.publish.enabled=false` + plugin Allure w `allure.properties`.

`src/test/resources/allure.properties`:

```properties
allure.results.directory=build/allure-results
allure.link.issue.pattern=https://jira.example.com/browse/{}
allure.link.tms.pattern=https://tms.example.com/case/{}
```

Używaj `@Step` na metodach pomocniczych, by raport czytał się jak spec. `@Attachment` dla screenshotów / response body. Severity przez mapowanie tagu Cucumber: `@severity_critical`, `@severity_blocker` itp., lub programowo `Allure.label("severity", "critical")`.

```java
package com.example.support;

import io.qameta.allure.Allure;
import io.qameta.allure.Attachment;
import io.qameta.allure.Step;
import io.restassured.response.Response;

public final class AllureReporter {

    @Step("Verify response matches contract")
    public void stepVerifyContract(final Response r) {
        // assertion logic
    }

    @Attachment(value = "Response body", type = "application/json")
    public byte[] attachResponse(final Response r) {
        return r.asByteArray();
    }

    @Attachment(value = "Screenshot", type = "image/png")
    public byte[] attachScreenshot(final byte[] png) { return png; }

    public void markCritical() {
        Allure.label("severity", "critical");
    }
}
```

W hookach, doczepiaj response przy failure:

```java
@After
public void dumpOnFailure(Scenario scenario) {
    if (scenario.isFailed() && ctx.getLastResponse() != null) {
        Allure.addAttachment("last-response.json",
            "application/json",
            ctx.getLastResponse().asString(),
            ".json");
    }
}
```

Źródła: `/allure-framework/allure-docs` (cucumber, junit5 frameworks pages), allure.properties config.

---

## 11. Standardy build Gradle

Kotlin DSL. Version catalog. JUnit Platform. Plugin Allure.

`gradle/libs.versions.toml`:

```toml
[versions]
junit       = "5.10.2"
cucumber    = "7.18.1"
restassured = "5.5.0"
playwright  = "1.49.0"
assertj     = "3.26.3"
allure      = "2.29.0"
slf4j       = "2.0.13"
logback     = "1.5.6"

[libraries]
junit-jupiter         = { module = "org.junit.jupiter:junit-jupiter", version.ref = "junit" }
junit-platform-suite  = { module = "org.junit.platform:junit-platform-suite", version = "1.10.2" }
cucumber-java         = { module = "io.cucumber:cucumber-java", version.ref = "cucumber" }
cucumber-pico         = { module = "io.cucumber:cucumber-picocontainer", version.ref = "cucumber" }
cucumber-junit-engine = { module = "io.cucumber:cucumber-junit-platform-engine", version.ref = "cucumber" }
restassured           = { module = "io.rest-assured:rest-assured", version.ref = "restassured" }
playwright            = { module = "com.microsoft.playwright:playwright", version.ref = "playwright" }
assertj               = { module = "org.assertj:assertj-core", version.ref = "assertj" }
allure-cucumber7      = { module = "io.qameta.allure:allure-cucumber7-jvm", version.ref = "allure" }
slf4j-api             = { module = "org.slf4j:slf4j-api", version.ref = "slf4j" }
logback-classic       = { module = "ch.qos.logback:logback-classic", version.ref = "logback" }
```

`build.gradle.kts`:

```kotlin
plugins {
    java
    id("io.qameta.allure") version "2.12.0"
}

java {
    toolchain {
        languageVersion = JavaLanguageVersion.of(17)
    }
}

repositories { mavenCentral() }

dependencies {
    testImplementation(libs.junit.jupiter)
    testImplementation(libs.junit.platform.suite)
    testImplementation(libs.cucumber.java)
    testImplementation(libs.cucumber.pico)
    testImplementation(libs.cucumber.junit.engine)
    testImplementation(libs.restassured)
    testImplementation(libs.playwright)
    testImplementation(libs.assertj)
    testImplementation(libs.allure.cucumber7)
    testImplementation(libs.slf4j.api)
    testRuntimeOnly(libs.logback.classic)
}

allure {
    version.set("2.29.0")
    adapter {
        autoconfigure.set(true)
        aspectjWeaver.set(true)
        frameworks {
            register("cucumber7Jvm")
        }
    }
}

tasks.withType<Test>().configureEach {
    useJUnitPlatform()
    maxParallelForks = (Runtime.getRuntime().availableProcessors() / 2).coerceAtLeast(1)

    systemProperty("cucumber.junit-platform.naming-strategy", "long")
    systemProperty("cucumber.execution.parallel.enabled", "true")
    systemProperty("cucumber.execution.parallel.config.strategy", "dynamic")
    systemProperty("cucumber.filter.tags",
        providers.systemProperty("cucumber.filter.tags").orElse("not @wip and not @known-bug").get())

    // Playwright JVM args (some platforms need this for shaded JNI)
    jvmArgs("-Xshare:off")

    testLogging {
        events("failed", "skipped")
        exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
    }
}
```

`src/test/resources/junit-platform.properties`:

```properties
cucumber.glue=com.example.steps,com.example.hooks
cucumber.plugin=pretty, io.qameta.allure.cucumber7jvm.AllureCucumber7Jvm
cucumber.publish.quiet=true
```

Źródła: `/gradle/gradle` (Kotlin DSL, version catalog, `useJUnitPlatform`, parallel forks), `/junit-team/junit-framework` (Suite engine, tag filtering), `/cucumber/cucumber-jvm` (junit-platform-engine config keys).

---

## 12. Logowanie i diagnostyka

Fasada SLF4J, impl Logback. Nigdy `System.out.println`. Nigdy `.printStackTrace()`. Niepowodzenia idą do Allure.

`src/test/resources/logback-test.xml`:

```xml
<configuration>
  <appender name="STDOUT" class="ch.qos.logback.core.ConsoleAppender">
    <encoder>
      <pattern>%d{HH:mm:ss.SSS} %-5level [%thread] %logger{36} - %msg%n</pattern>
    </encoder>
  </appender>
  <root level="INFO">
    <appender-ref ref="STDOUT"/>
  </root>
  <logger name="io.restassured" level="WARN"/>
  <logger name="com.microsoft.playwright" level="INFO"/>
</configuration>
```

Używaj idiomu lazy logging (bez konkatenacji string poza ramą logu):

```java
private static final org.slf4j.Logger log = org.slf4j.LoggerFactory.getLogger(UserApiClient.class);

public Response create(final String email, final String role) {
    log.info("Create user request: email={}, role={}", email, role);
    final Response r = given().spec(reqSpec)
            .body(new CreateUserRequest(email, role))
            .post("/users");
    if (r.statusCode() >= 400) {
        log.warn("Create user failed: status={}, body={}", r.statusCode(), r.asString());
    }
    return r;
}
```

Przy niepowodzeniu scenariusza dołącz ostatni response + screenshot. Patrz hooki w Sek. 5 i Allure w Sek. 10.

Źródła: SLF4J user manual, Logback config docs.

---

## 13. Styl kodu

- **Google Java Format** (lub plugin Spotless wymuszający to samo). Jeden gate CI.
- `final` na lokalnych + parametrach, gdy nie szkodzi czytelności — sygnalizuje "no reassignment".
- `record` dla niezmiennych typów wartościowych (DTO, requesty, paramy).
- `Optional<T>` tylko jako **return type** — nigdy field, nigdy parameter.
- Null-safety: preferuj `Objects.requireNonNull` na granicy konstruktora.
- Bez wildcard importów. Bez star statics, z wyjątkiem frameworków asercji (`assertThat`, `given`).
- Szerokość linii 120. Wcięcie 4 spacje.

```java
package com.example.support;

import java.util.Objects;
import java.util.Optional;

public final class EnvConfig {
    private final String baseUrl;
    private final String adminToken;

    public EnvConfig(final String baseUrl, final String adminToken) {
        this.baseUrl = Objects.requireNonNull(baseUrl, "baseUrl");
        this.adminToken = Objects.requireNonNull(adminToken, "adminToken");
    }

    public String baseUrl() { return baseUrl; }

    public Optional<String> adminTokenIfPresent() {
        return Optional.ofNullable(adminToken).filter(s -> !s.isBlank());
    }
}
```

Snippet Spotless dla `build.gradle.kts`:

```kotlin
plugins { id("com.diffplug.spotless") version "6.25.0" }
spotless {
    java {
        googleJavaFormat("1.22.0")
        target("src/**/*.java")
    }
}
```

Źródła: [Google Java Style](https://google.github.io/styleguide/javaguide.html), Spotless plugin docs.

---

## 14. Strategia danych testowych

Czysta izolacja scenariusza — brak współdzielonego mutowalnego stanu. Każdy scenariusz buduje własny fixture, sprząta w `@After`.

Wzorce:

- **Builder** dla czytelnych fixtures.
- **Faker** (Datafaker `net.datafaker:datafaker`) dla losowości — unika kolizji w runach równoległych.
- **Cleanup hook** zakresowany przez scenariusz. Śledź utworzone ID w `ScenarioContext`, usuń w `@After`.
- **Idempotentny prefix**, np. `test-${scenarioId}-` — łatwy cleanup DB, jeśli hook nie zadziałał.

```java
package com.example.support;

import net.datafaker.Faker;

public final class UserBuilder {
    private static final Faker FAKER = new Faker();

    private String email = "test-" + FAKER.internet().uuid() + "@example.com";
    private String role  = "USER";
    private boolean active = true;

    public static UserBuilder aUser() { return new UserBuilder(); }

    public UserBuilder withEmail(final String e) { this.email = e; return this; }
    public UserBuilder withRole(final String r)  { this.role = r; return this; }
    public UserBuilder inactive()                { this.active = false; return this; }

    public CreateUserRequest build() { return new CreateUserRequest(email, role); }
    public String email() { return email; }
}
```

Cleanup hook:

```java
public final class CleanupHooks {
    private final ScenarioContext ctx;
    private final UserApiClient userApi;

    public CleanupHooks(ScenarioContext ctx, UserApiClient userApi) {
        this.ctx = ctx;
        this.userApi = userApi;
    }

    @After(order = 100) // higher order runs LATER → cleanup is last
    public void purgeCreatedUsers() {
        ctx.getCreatedUserIds().forEach(id -> {
            try { userApi.deleteById(id); }
            catch (Exception ignored) { /* best-effort */ }
        });
    }
}
```

Źródła: `/cucumber/cucumber-jvm` (Hook order), AssertJ patterns, Datafaker docs.

---

## 15. Komendy CLI

Codzienne komendy. Zapamiętaj pierwsze trzy.

```bash
# Run everything
./gradlew test

# Smoke only — < 30s sanity
./gradlew test -Dcucumber.filter.tags="@smoke"

# Critical bucket
./gradlew test -Dcucumber.filter.tags="@critical and not @extended"

# Single feature file
./gradlew test -Dcucumber.features="src/test/resources/features/api/user-creation.feature"

# Single scenario by name (regex)
./gradlew test -Dcucumber.filter.name=".*Admin creates active user.*"

# Tag combination — security regression, drop @known-bug
./gradlew test -Dcucumber.filter.tags="@security and @regression and not @known-bug"

# Run runner class directly
./gradlew test --tests "com.example.runners.RunSmokeTest"

# Allure — generate static report
./gradlew allureReport
# Allure — open live in browser (preferred during debugging)
./gradlew allureServe

# Spotless — apply formatting
./gradlew spotlessApply
# Spotless — verify only (CI gate)
./gradlew spotlessCheck

# Refresh dependencies (post lockfile change)
./gradlew --refresh-dependencies build

# Parallel + max heap, useful when suite grows
./gradlew test --parallel -Dorg.gradle.jvmargs="-Xmx2g"
```

Ściąga DSL tagów — wyrażenia Cucumber w `cucumber.filter.tags`:

```
@smoke                        # has @smoke
@smoke and @critical          # both
@smoke and not @extended      # has smoke, lacks extended
(@smoke or @critical) and not @known-bug
```

Źródła: `/cucumber/cucumber-jvm` (filter.tags property), `/gradle/gradle` (test task CLI).

---

Ostatnia aktualizacja: 2026-05-08 (research via Context7 + WebSearch)
