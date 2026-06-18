# Troubleshooting

Status: active

| Objaw                                                          | Co sprawdzić                                                                                       |
|----------------------------------------------------------------|----------------------------------------------------------------------------------------------------|
| `ConfigError: invalid config ...`                              | `_validate` rzuca pełną listę pól. Sprawdź opcjonalne klucze v2 w `agentic-os.yml.example`.        |
| `POST /api/config` -> 403                                      | Ustaw `dashboard.enable_write_endpoints` na `true` i zrestartuj `up`.                              |
| `POST /api/config` -> 400 `dashboard.host`                     | Decyzja operatora wymagana, żeby zmienić host z 127.0.0.1. Edytuj YAML bezpośrednio.               |
| `sut-start` -> exit 2 `infra_missing_docker`                   | Brak `docker` na PATH. Zainstaluj Docker albo wyłącz `sut.autostart`.                              |
| `sut-start` -> exit 2 `infra_missing_compose_file`             | `sut.compose_file` wskazuje na nieistniejący plik. Sprawdź `ls`.                                   |
| `healthcheck` -> exit 2 `infra_healthcheck_timeout`            | Aplikacja nie staje się healthy. Sprawdź `agentic-os-runtime/logs/subprocess/sut-healthcheck-*.log`.      |
| `task abandon-patch` -> `UsageError: no patch artifact ...`    | Ścieżka patcha nie zgadza się z `work_item_artifacts.path`. `task show <id>` listuje artefakty.    |
| Plan gate REJECT `expected_assertion is trivial`               | Plan item ma `response.ok` / `status 2xx` jako jedyną asercję. Podaj konkretny HTTP code + body.   |
| Generator API zwraca `UsageError: missing source_ref`          | Plan item bez `source_refs[]`. Dodaj co najmniej jedno `docs/...` lub `OpenAPI` ref.               |
| Reviewer model -> ValueError `gate output is too short`        | Model nie zwrócił strict APPROVE/REJECT format. Sprawdź `agentic-os-runtime/model-outputs/<id>.txt`.      |
| `pragma foreign_key_check` zwraca wiersze                      | `run recovery` powinien je posprzątać. Jeśli nadal — backup state.db, otwórz issue.                |
| `BUG-NNN` ma duplikat                                          | `next_bug_id(existing)` wybiera max+1. Zweryfikuj, że stare bugi nie zostały skasowane.            |

## Logi do podejrzenia

- `agentic-os-runtime/logs/orchestrator.log` — decyzje runtime.
- `agentic-os-runtime/logs/dashboard.log` — HTTP server.
- `agentic-os-runtime/logs/subprocess/<run-id>.log` — pojedyncze wywołania (run-tests, sut-*).
- `agentic-os-runtime/model-inputs/<id>.txt` — prompt wysłany do modelu (po redakcji).
- `agentic-os-runtime/model-outputs/<id>.txt` — pełne stdout modelu.
- `agentic-os-runtime/evidence/<run-id>/` — manifesty + zrzuty + trace.

## Recovery checklist

1. `git status --short --branch`.
2. `.venv/bin/python -m pytest tests/test_runtime_guards.py` — krytyczne guardrails.
3. `./scripts/agentic-os.sh --json run recovery`.
4. `sqlite3 agentic-os-runtime/state.db "pragma foreign_key_check;"`.
5. `./run-tests.sh` — pełny suite frameworka.
6. `./scripts/agentic-os.sh run final-gate`.
