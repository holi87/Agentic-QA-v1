// @ts-check
// C2 lint gate (issue #364) — static enforcement of the Playwright + TypeScript
// coding standards (docs/standards/playwright-ts-standards.md §5–§6). Shipped in
// every generated bundle (standards §6: "enforced by the C2 lint ruleset"), and
// run-tests.sh fails the build on a violation. Non-type-aware on purpose: every
// rule below is syntactic, so no `parserOptions.project` (keeps lint fast and
// free of a project-path dependency); `strict` typing is enforced separately by
// `tsc --noEmit`.
import js from '@eslint/js';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['node_modules/', 'playwright-report/', 'test-results/'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.ts'],
    rules: {
      // TypeScript's own checker resolves identifiers; `no-undef` only produces
      // false positives on TS (Node/Playwright globals) — off per the
      // typescript-eslint guidance.
      'no-undef': 'off',

      // §6 — size & structure limits. A file/function over budget is a
      // decomposition signal (split the Page Object / client). Blank lines and
      // comments are excluded so the budgets measure real code.
      'max-lines': ['error', { max: 300, skipBlankLines: true, skipComments: true }],
      'max-lines-per-function': ['error', { max: 40, skipBlankLines: true, skipComments: true }],
      'max-depth': ['error', 3],

      // §6 — no `any` (strict TS is enforced by `tsc --noEmit`).
      '@typescript-eslint/no-explicit-any': 'error',

      // Dead-code signal, tuned for no noise (issue #364): flag unused
      // vars/args/imports, but `_`-prefixed names are intentional and a caught
      // error swallowed by a fail-soft block (the generators' coverage-floor
      // a11y / link-walk probes) is deliberate — not dead code.
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrors: 'none' },
      ],

      // §5 — hard waits are forbidden; wait on a condition (locator state,
      // response, URL), not the clock.
      'no-restricted-syntax': [
        'error',
        {
          selector: "CallExpression[callee.property.name='waitForTimeout']",
          message:
            'Hard waits are forbidden (standards §5) — wait on a locator/response/URL condition, not the clock.',
        },
      ],
    },
  },
);
