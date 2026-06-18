# Pretask: cover the Fake Todos API

The fake Todos API is documented in `openapi.yaml` (sibling file). It
exposes a small CRUD surface with bearer auth on the write endpoints:

- `GET /todos` — list, optionally filtered by `completed`.
- `POST /todos` — create. Requires bearer token. Validates `title`.
- `GET /todos/{id}` — fetch one. Returns 404 for unknown ids.
- `PUT /todos/{id}` — replace. Requires bearer token. Validates body.
- `DELETE /todos/{id}` — delete. Requires bearer token.

## What to cover

This pretask asks for test candidates that exercise the contract from
the OpenAPI spec, not the internal storage. Specifically:

1. Happy-path CRUD: create → list → get → update → delete, asserting
   status codes, response shape, and `Content-Type`.
2. Auth on each write endpoint: missing token → 401, invalid token →
   401, valid token → 2xx.
3. Validation: `title` longer than 200 characters → 400 on create and
   update; missing `title` → 400.
4. Edge cases on `/todos/{id}`: non-existent id → 404 (get, update,
   delete); negative or non-integer id → 4xx (no 500).
5. Filtering: `GET /todos?completed=true` and `?completed=false`
   return correctly filtered subsets; unknown query keys are ignored
   (no 500).

## Out of scope

- Performance, concurrency, idempotency beyond the contract above.
- Database persistence — the SUT is a fixture, expected to reset
  between runs.

## Why this fixture exists

This pretask is the input half of the RC proof fixture (issue #137).
Running `examples/fake-sut/run-rc-proof.sh` synthesises a work item
from this file, runs analyse + plan, and asserts the pipeline produces
sensible candidates / TEST-PLAN.json without a model or network call.
