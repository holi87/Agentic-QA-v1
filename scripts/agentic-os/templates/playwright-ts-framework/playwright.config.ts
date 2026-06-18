import { defineConfig } from '@playwright/test';

/**
 * Standalone config for the Agentic-OS-generated suite (issue #369).
 *
 * The generated specs read their target URLs from the environment
 * (`API_BASE_URL` / `UI_BASE_URL`) and self-skip when a prerequisite env var
 * is absent — so this config carries NO hard-coded baseURL. Point the suite at
 * a SUT by exporting those vars before `npx playwright test` (see README).
 *
 * Reports land in `playwright-report/` (HTML) and `test-results/results.xml`
 * (JUnit) so the OS / CI can surface them (issues #371–#373).
 */
export default defineConfig({
  // Scan the whole bundle for `*.spec.ts`, not just `tests/`. The assembler
  // honors each spec's relative path verbatim, and a SUT with a non-default
  // `sut.tests_dir` (e.g. `qa/integration`) lands specs under that path — a
  // narrow `testDir: 'tests'` would then report "no tests" (issue #369 review).
  // Playwright ignores `node_modules` automatically, so '.' is safe.
  testDir: '.',
  testMatch: '**/*.spec.ts',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [
    ['list'],
    ['html', { outputFolder: 'playwright-report', open: 'never' }],
    ['junit', { outputFile: 'test-results/results.xml' }],
  ],
  use: {
    // Web-first debugging artifacts on failure — per the PW+TS coding
    // standards (docs/standards/playwright-ts-standards.md §5).
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
});
