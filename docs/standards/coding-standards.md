# Coding Standards — Java BDD Stack (Cucumber + Playwright + RestAssured)

Status: superseded
Superseded-on: 2026-05-29
Superseded-by: ADR-0002
Reason: the canonical generated stack is Playwright + TypeScript (the Java BDD stack below was inherited from a previous project). PW+TS coding standards are tracked under #366.

> **Superseded.** This document describes the **inherited** Java BDD stack.
> The canonical generated stack is now **Playwright + TypeScript** — see
> `ADR-0002`. It is kept
> for historical context until the Playwright + TS standards land (#366); do
> not treat it as the current contract.

This file = HOW we write code. Sister doc `qa-standards.md` = WHAT quality bar (ISO 25010, ISO/IEC/IEEE 29119, ISTQB, OWASP API Top 10, WCAG 2.2). Always read both. coding-standards prescribes structure, names, idioms; qa-standards prescribes coverage targets, severity, traceability. When conflict — qa-standards wins on intent, coding-standards wins on syntax.

Stack assumed: Java 17 LTS, Gradle 8.x Kotlin DSL, JUnit 5 Jupiter, Cucumber JVM 7.x + Picocontainer + cucumber-junit-platform-engine, RestAssured 5.x, Playwright for Java, AssertJ + SoftAssertions, Allure 2.x.

---

## 1. SOLID & Clean Code

Rules. Not negotiable in 8h hackathon — they save time, not cost it.

- **SRP per step def class.** One feature area = one step def class. Auth steps != Order steps.
- **DI via Picocontainer.** No `static`, no singletons, no `ThreadLocal` for scenario state. Constructor injection only.
- **DIP.** Steps depend on abstractions (`UserApiClient`, `LoginPage`), not on `RequestSpecification` or `Page` directly.
- **No business logic in steps.** Step = parse Gherkin + delegate. Logic lives in clients/page objects.
- **No shared mutable static state.** Parallel scenarios will eat you alive.

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

Sources: `/cucumber/cucumber-jvm` (PicoContainer step injection), Robert C. Martin — Clean Code Ch.3 (Functions), Ch.10 (Classes).

---

## 2. Project Structure

Gradle module layout with **explicit framework / tests split**. The framework half lives in
`src/main/java` and is owned by the framework (changes go through `unitTest` gates). The
project test half lives in `src/test/java` and is freely replaceable per project.

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

Why this layout:

- **Cucumber glue still works.** Gradle's `test` source set has `main` on its classpath by
  default, so `Hooks`, `AssertionHook`, and `PlaywrightContext` are auto-discovered when
  the runner scans the `pl.qualitycat` glue package.
- **Framework changes have their own gate.** `./gradlew unitTest` validates framework
  classes in isolation. Project tests can break without invalidating framework correctness.
- **Project tests stay focused.** `src/test/java` contains only project-specific code.
  Reviewers and humans without context can navigate it in seconds.
- **Future-proof.** Framework half can be extracted into a published artifact without
  shuffling files — package paths and visibility are already correct.

Sources: `/cucumber/cucumber-jvm` (junit-platform-engine layout), `/gradle/gradle` userguide — Java testing project layout.

---

## 3. Naming Conventions

Pick one rule, apply everywhere. Reviewers should never wonder.

| Artifact | Convention | Example |
|---|---|---|
| Step def class | `<Domain>Steps` | `UserSteps.java`, `LoginSteps.java` |
| Page object | `<Page>Page` | `LoginPage.java`, `DashboardPage.java` |
| API client | `<Resource>ApiClient` | `UserApiClient.java`, `OrderApiClient.java` |
| Hooks class | `Hooks` or `<Domain>Hooks` | `Hooks.java`, `DbHooks.java` |
| Runner | `Run<Bucket>Test` | `RunSmokeTest.java`, `RunRegressionTest.java` |
| Feature file | kebab-case nouns | `user-creation.feature`, `login-negative.feature` |
| Scenario | imperative, business-language | `Scenario: Admin creates active user` |
| Step method | `verbObject` camelCase | `adminCreatesUser`, `userShouldBeActive` |
| Test data builder | `<Entity>Builder` | `UserBuilder.java` |

Step methods do NOT need to mirror Gherkin word-for-word — Cucumber binds via annotation regex, not method name. So make method names code-readable.

```java
@When("admin creates user {string} with role {string}")
public void adminCreatesUserWithRole(final String email, final String role) { ... }
```

Sources: Google Java Style Guide §5 (Naming), `/cucumber/docs` Gherkin reference.

---

## 4. Cucumber BDD Standards

Gherkin in **English**. Business language. No "click button". No technical leakage.

Rules:

- **Feature** = one capability. One file. ≤ 10 scenarios.
- **Background** = preconditions shared by ALL scenarios in file. Not "stuff I'm too lazy to repeat".
- **Scenario Outline** when same flow with different data; **Data Table** when single scenario takes structured input.
- **One runner per tag bucket** — `RunSmokeTest`, `RunCriticalTest`, `RunRegressionTest`. Compose via `cucumber.filter.tags`.
- Step reuse: write 100 short steps before 10 god-steps.

Tag taxonomy (mandatory — qa-standards Sec. tagging):

| Tag | Meaning |
|---|---|
| `@smoke` | 3-5 tests, SUT alive, < 30s |
| `@critical` | core business logic |
| `@regression` | every test (full suite) |
| `@negative` | 4xx/5xx, invalid input |
| `@boundary` | edge values (0, MAX_INT, empty string) |
| `@security` | OWASP top 10 (IDOR, injection, mass assignment) |
| `@functional-<area>` | per feature/domain (`@functional-users`) |
| `@extended` | parametrization, deeper variants |

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

Data table vs Outline — table when scenario takes a single structured input set; outline when same scenario re-runs with different rows.

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

Sources: `/cucumber/docs` (Gherkin reference), `/cucumber/cucumber-jvm` (junit-platform suite filter tags).

---

## 5. Step Definitions

Steps are thin. Each step:

1. Parse args from Gherkin.
2. Call into client/PO.
3. Persist result in `ScenarioContext`.

No `if`, no loops over assertion logic, no `try/catch` swallowing. Hooks only for setup/teardown — never assertion.

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

Sources: `/cucumber/cucumber-jvm` — Hooks API, PicoContainer step DI.

---

## 6. Page Object Model — Playwright Java

Locator strategy — accessibility first, never CSS class chains.

Priority: `getByRole` > `getByLabel` > `getByPlaceholder` > `getByText` > `getByTestId` > CSS.

Rules:
- One PO per page (or significant component).
- Constructor takes `Page` (Playwright page).
- **No `Thread.sleep`.** Playwright auto-waits — use `assertThat(locator).isVisible()` / `.waitFor()`.
- POs expose actions and queries. Assertions live in steps (not PO), unless component-level web-first assert.
- `Locator` fields are `final`. No mutation after construct.

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

Sources: `/microsoft/playwright-java` (locators, PlaywrightAssertions auto-wait), `/websites/playwright_dev` (Best Practices — locators).

---

## 7. API Layer — RestAssured

Never compose URLs by string concat. Never inline base URI in tests. One `RequestSpecBuilder` + `ResponseSpecBuilder` shared per client.

Rules:
- Logging: `LogDetail.IF_VALIDATION_FAILS` only — silent on green, full dump on red.
- Config from env / properties — never hardcoded host.
- One client class per resource (`UserApiClient`, `OrderApiClient`).
- Return typed DTOs or `Response` — let steps decide.

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

`record` for request/response bodies — immutable, no Lombok needed:

```java
public record CreateUserRequest(String email, String role) {}
public record UserDto(String id, String email, String role, boolean active) {}
```

Sources: [RestAssured Wiki — Usage](https://github.com/rest-assured/rest-assured/wiki/usage), [RestAssured RequestSpecBuilder source](https://github.com/rest-assured/rest-assured/blob/master/rest-assured/src/main/java/io/restassured/builder/RequestSpecBuilder.java), [REST-assured Best Practices Guide](https://github.com/merridewberry/rest-assured-best-practices).

---

## 8. Assertions — AssertJ + SoftAssertions

Two assertion classes. Never mix.

- **BIZ (business)** — what user/contract demands. Status code is correct, returned `email` matches input, `active=true`. Allure description starts with `BIZ:`.
- **TECH (technical)** — schema valid, response time < SLO, header `Content-Type` correct. Allure description starts with `TECH:`.

Use `SoftAssertions` per scenario step that asserts > 1 thing → capture all failures, not just first.

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

`as("BIZ: ...")` description ends up in Allure failure header — auditor reads it. Worth the keystrokes.

Sources: `/assertj/assertj` (SoftAssertions, describedAs), Allure cucumber docs.

---

## 9. Bug-Aware Testing (CRITICAL)

**Assertions express the SPEC, not the SUT current behavior.** Never write `.isEqualTo(actualWeirdValue)` because `.isEqualTo(specValue)` is red. That's adjusting the test to make it green = hiding bugs. In QualityCat stakeholders plant bugs deliberately. Detection > pass rate.

Workflow when red:

1. Re-read spec / API doc / OpenAPI.
2. If SUT behavior contradicts spec → **bug**. Keep assertion correct. Tag scenario `@known-bug @bug-NNN` or move test to `@skip` ONLY with reason. Create `bugs/BUG-NNN-<slug>.md` via `/QC-claude-report-bug`.
3. If spec ambiguous → mark `@ambiguous`, note as `severity: Info` in `bugs/BUG-NNN-<slug>.md`, default to strictest reasonable interpretation.
4. If test wrong (wrong endpoint, wrong payload) → fix test.

Never silently flip an assertion. Never `// TODO fix later` on a red test.

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

`bugs/BUG-NNN-<slug>.md` entry template (per `bug-reporting.md` schema):

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

Sources: ISTQB FL §1.4 (defects vs failures), qa-standards.md.

---

## 10. Allure Integration

Dependency: `io.qameta.allure:allure-cucumber7-jvm`. Use `cucumber-junit-platform-engine`, plug Allure via `META-INF/services` (auto) or `cucumber.publish.enabled=false` + Allure plugin in `allure.properties`.

`src/test/resources/allure.properties`:

```properties
allure.results.directory=build/allure-results
allure.link.issue.pattern=https://jira.example.com/browse/{}
allure.link.tms.pattern=https://tms.example.com/case/{}
```

Use `@Step` on helper methods to make report read like spec. `@Attachment` for screenshots / response bodies. Severity via Cucumber tag mapping: `@severity_critical`, `@severity_blocker` etc., or programmatic `Allure.label("severity", "critical")`.

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

In hooks, attach response on failure:

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

Sources: `/allure-framework/allure-docs` (cucumber, junit5 frameworks pages), allure.properties config.

---

## 11. Gradle Build Standards

Kotlin DSL. Version catalog. JUnit Platform. Allure plugin.

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

Sources: `/gradle/gradle` (Kotlin DSL, version catalog, `useJUnitPlatform`, parallel forks), `/junit-team/junit-framework` (Suite engine, tag filtering), `/cucumber/cucumber-jvm` (junit-platform-engine config keys).

---

## 12. Logging & Diagnostics

SLF4J facade, Logback impl. Never `System.out.println`. Never `.printStackTrace()`. Failures attach to Allure.

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

Use lazy logging idiom (no string concat outside log frame):

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

On scenario failure, attach last response + screenshot. See Sec. 5 hooks and Sec. 10 Allure.

Sources: SLF4J user manual, Logback config docs.

---

## 13. Code Style

- **Google Java Format** (or Spotless plugin enforcing same). One CI gate.
- `final` on locals + parameters where it doesn't hurt readability — signals "no reassignment".
- `record` for immutable value types (DTOs, requests, params).
- `Optional<T>` only as **return type** — never field, never parameter.
- Null-safety: prefer `Objects.requireNonNull` at constructor boundary.
- No wildcard imports. No star statics except for assertion frameworks (`assertThat`, `given`).
- Line width 120. Indent 4 spaces.

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

Spotless snippet for `build.gradle.kts`:

```kotlin
plugins { id("com.diffplug.spotless") version "6.25.0" }
spotless {
    java {
        googleJavaFormat("1.22.0")
        target("src/**/*.java")
    }
}
```

Sources: [Google Java Style](https://google.github.io/styleguide/javaguide.html), Spotless plugin docs.

---

## 14. Test Data Strategy

Pure scenario isolation — no shared mutable state. Every scenario builds its own fixture, cleans on `@After`.

Patterns:

- **Builder** for readable fixtures.
- **Faker** (Datafaker `net.datafaker:datafaker`) for randomness — avoids collisions in parallel runs.
- **Cleanup hook** scoped by scenario. Track created IDs in `ScenarioContext`, delete in `@After`.
- **Idempotent prefix**, e.g. `test-${scenarioId}-` — easy DB cleanup if hook missed.

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

Sources: `/cucumber/cucumber-jvm` (Hook order), AssertJ patterns, Datafaker docs.

---

## 15. CLI Commands

Daily commands. Memorize the first three.

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

Tag DSL cheat — Cucumber expressions in `cucumber.filter.tags`:

```
@smoke                        # has @smoke
@smoke and @critical          # both
@smoke and not @extended      # has smoke, lacks extended
(@smoke or @critical) and not @known-bug
```

Sources: `/cucumber/cucumber-jvm` (filter.tags property), `/gradle/gradle` (test task CLI).

---

Last updated: 2026-05-08 (research via Context7 + WebSearch)
