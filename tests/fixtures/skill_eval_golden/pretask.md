# Identity API — QA pretask

## Business goal

Ensure the public identity surface (auth + user management) remains
correct, secure, and resilient against credential-stuffing and invalid
input. This is the gating contract every downstream service relies on.

## Expected behavior

- `POST /auth/login` issues a bearer token for valid credentials and
  rejects invalid ones with HTTP 401. Throttled requests return HTTP 429.
- `POST /auth/refresh` and `POST /auth/logout` require a valid bearer
  token; missing or expired tokens return HTTP 401.
- `GET /users` lists users; `POST /users` creates with strict
  validation (email format, password length ≥12, role enum) and rejects
  duplicates with HTTP 409.
- `GET|PUT|DELETE /users/{id}` succeed for an existing UUID and return
  HTTP 404 otherwise. `PUT` enforces field validation (400 on bad role).
- `POST /users/{id}/password-reset` is idempotent — repeating the call
  must not multiply outgoing emails.

## Relevant surfaces

- `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`
- `GET /users`, `POST /users`
- `GET /users/{id}`, `PUT /users/{id}`, `DELETE /users/{id}`
- `POST /users/{id}/password-reset`

## Constraints

- No real user records; all fixtures must be cleaned up after the run.
- Bearer token must be sourced from an env-only secret, never hard-coded.
- Tests must cover both happy and unhappy paths for every endpoint, plus
  validation-error and unauthorized-error paths where the spec lists them.
