# Troubleshooting

Status: active

| Symptom                                                        | What to check                                                                                      |
|----------------------------------------------------------------|----------------------------------------------------------------------------------------------------|
| `ConfigError: invalid config ...`                              | `_validate` raises the full field list. Check the optional v2 keys in `agentic-os.yml.example`.    |
| `POST /api/config` -> 403                                      | Set `dashboard.enable_write_endpoints` to `true` and restart `up`.                                 |
| `POST /api/config` -> 400 `dashboard.host`                     | Operator decision required to change host away from 127.0.0.1. Edit the YAML directly.             |
| `sut-start` -> exit 2 `infra_missing_docker`                   | No `docker` on PATH. Install Docker or disable `sut.autostart`.                                    |
| `sut-start` -> exit 2 `infra_missing_compose_file`             | `sut.compose_file` points at a missing file. Verify with `ls`.                                     |
| `healthcheck` -> exit 2 `infra_healthcheck_timeout`            | The app never becomes healthy. Check `agentic-os-runtime/logs/subprocess/sut-healthcheck-*.log`.          |
| `task abandon-patch` -> `UsageError: no patch artifact ...`    | The patch path does not match `work_item_artifacts.path`. `task show <id>` lists artifacts.        |
| Plan gate REJECT `expected_assertion is trivial`               | Plan item has `response.ok` / `status 2xx` as the only assertion. Provide a concrete HTTP code + body. |
| API generator returns `UsageError: missing source_ref`         | Plan item has no `source_refs[]`. Add at least one `docs/...` or `OpenAPI` ref.                    |
| Reviewer model -> ValueError `gate output is too short`        | The model did not return strict APPROVE/REJECT format. Check `agentic-os-runtime/model-outputs/<id>.txt`. |
| `pragma foreign_key_check` returns rows                        | `run recovery` should clean them up. If it does not — back up state.db and open an issue.          |
| `BUG-NNN` is duplicated                                        | `next_bug_id(existing)` picks max+1. Verify that old bugs have not been deleted.                   |

## Logs to inspect

- `agentic-os-runtime/logs/orchestrator.log` — runtime decisions.
- `agentic-os-runtime/logs/dashboard.log` — HTTP server.
- `agentic-os-runtime/logs/subprocess/<run-id>.log` — individual invocations (run-tests, sut-*).
- `agentic-os-runtime/model-inputs/<id>.txt` — prompt sent to the model (post-redact).
- `agentic-os-runtime/model-outputs/<id>.txt` — full model stdout.
- `agentic-os-runtime/evidence/<run-id>/` — manifests + screenshots + traces.

## Recovery checklist

1. `git status --short --branch`.
2. `.venv/bin/python -m pytest tests/test_runtime_guards.py` — critical guardrails.
3. `./scripts/agentic-os.sh --json run recovery`.
4. `sqlite3 agentic-os-runtime/state.db "pragma foreign_key_check;"`.
5. `./run-tests.sh` — full framework suite.
6. `./scripts/agentic-os.sh run final-gate`.
