# Fake SUT — RC proof fixture

Self-contained proof that the Agentic OS pipeline works on a fresh
checkout, without any model call or network access. Closes the
`pending` line in `docs/operator-guide.md:119` (issue #137).

## What's inside

```
examples/fake-sut/
├── README.md            ← this file
├── openapi.yaml         ← Todos API spec (5 operations, bearer auth)
├── pretask.md           ← operator pretask describing what to test
├── server.py            ← optional FastAPI-shaped stdlib server (online half)
└── run-rc-proof.py      ← orchestrator — runs the offline proof
```

## Run the offline proof

```bash
python examples/fake-sut/run-rc-proof.py
```

The script:

1. Creates a temp workspace (or uses the path you pass as `argv[1]`).
2. Seeds `openapi.yaml`, `pretask/pretask.md`, and the config
   template.
3. Runs `agentic-os init --force`.
4. Patches the config to add `sut.openapi.sources` pointing at the
   spec.
5. Runs `agentic-os inbox synthesize` → creates a work item from the
   pretask.
6. Runs `agentic-os task analyze <id>` → produces `sut-map.json`,
   `candidate-tests.{md,json}`.
7. Runs `agentic-os task plan <id>` → produces `TEST-PLAN.{md,json}`.
8. Runs `agentic-os run dry-run --fake-sut` → produces
   `reports/last-run.json` + `reports/summary.md`.
9. Asserts artifacts exist with sensible shape (≥4 parsed OpenAPI
   operations, ≥3 candidates, ≥1 plan item, `discovery_only` flag on
   the report).

On success: `RC PROOF: PASS` and the temp workspace is removed.
On failure: workspace is left for inspection at the path printed.

The proof intentionally **stops before `task implement-tests`** —
generating executable tests needs a real LLM. The online half below
covers that manually.

## Run the online half (optional)

The online half is not automated because it needs a running SUT and
an LLM with credentials.

1. Start the fake server:

   ```bash
   python examples/fake-sut/server.py --port 8001 --token secret
   ```

2. Inside a workspace created by the offline proof (pass an explicit
   path so it stays around):

   ```bash
   python examples/fake-sut/run-rc-proof.py /tmp/agentic-rc-ws
   cd /tmp/agentic-rc-ws
   ```

3. Approve a candidate and ask Agentic OS to generate executable
   tests:

   ```bash
   agentic-os task candidates <work-item-id>
   agentic-os task approve-candidate <work-item-id> <candidate-id>
   agentic-os task implement-tests <work-item-id>
   ```

4. Run the generated tests against the fake server, then exercise the
   report path:

   ```bash
   agentic-os run run-tests
   agentic-os run final-gate
   ```

The bear minimum a healthy online run produces: `reports/last-run.json`
with `total > 0`, `passed > 0`, plus any `bugs/BUG-NNN-*.md` for
discovered defects.

## Why this fixture exists

Before this fixture, operators had no single command that proved the
local environment was actually wired correctly. `docs/operator-guide.md`
listed the RC proof as `pending`. With this fixture:

- New operators run one command to verify their checkout.
- CI runs the offline proof as `tests/test_fake_sut_proof.py` so any
  regression in the deterministic half (analyse / plan / fake-sut
  report) breaks immediately.
- The online half is documented as a follow-up the operator can drive
  by hand once they have model credentials.
