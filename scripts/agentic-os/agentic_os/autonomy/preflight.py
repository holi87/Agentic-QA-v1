"""Pre-start environment + config checks (issue #292)."""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set

from .. import task_synthesis
from ..events import EventLog, event_log_for_paths
from ..paths import RuntimePaths
from ..runtime.tuning import (
    EVENTS_LOG_RING_SIZE as _EVENTS_LOG_RING_SIZE,
    RECORD_DETAIL_MAX_CHARS as _RECORD_DETAIL_MAX_CHARS,
    SHUTDOWN_GRACE_SECONDS as _SHUTDOWN_GRACE_SECONDS,
)
from ..storage import init_db
# preflight→exploratory is a cycle (exploratory→session_state→preflight); lazy below (#292).



def preflight_check(paths: RuntimePaths) -> Dict[str, Any]:
    """Validate prerequisites before starting an autonomy session.

    Returns a structured payload the UI can render as a checklist or
    wizard. Each check has `id`, `status` (`pass` | `warn` | `fail`),
    `message`, and `actions` (short list of operator-facing remediations).
    `ok` is True only when every check passes.
    """
    from ..config import ConfigError, load_or_default
    from ..sut_discovery import discover_sut

    checks: List[Dict[str, Any]] = []

    try:
        cfg = load_or_default(paths.repo_root)
    except ConfigError as exc:
        checks.append({
            "id": "config",
            "status": "fail",
            "message": f"config invalid: {exc}",
            "actions": [
                "Fix `config/agentic-os.yml` — see `docs/cli-contract.md` for the schema.",
                "Run `./scripts/agentic-os.sh --json doctor --sut --docker --models`.",
            ],
        })
        return {"ok": False, "checks": checks}

    checks.append({
        "id": "config",
        "status": "pass",
        "message": "config loads cleanly",
        "actions": [],
    })

    sut_cfg = (cfg.raw.get("sut") or {}) if isinstance(cfg.raw, dict) else {}
    from .exploratory import _online_endpoint_urls  # lazy — see top-of-module note
    online_urls = _online_endpoint_urls(cfg.raw)
    if sut_cfg.get("mode") == "online":
        if not online_urls:
            checks.append({
                "id": "sut_online",
                "status": "fail",
                "message": "sut.mode=online but no enabled web/api URL is configured",
                "actions": [
                    "Enable `sut.web` or `sut.api` and provide a URL in `config/agentic-os.yml`.",
                ],
            })
        else:
            for surface, url in sorted(online_urls.items()):
                checks.append({
                    "id": f"sut_online_{surface}",
                    "status": "pass",
                    "message": f"online {surface} URL configured: {url}",
                    "actions": [],
                })
    else:
        sut_root_str = sut_cfg.get("root") or "."
        sut_root = (paths.repo_root / sut_root_str).resolve()
        if not sut_root.exists():
            checks.append({
                "id": "sut_root",
                "status": "fail",
                "message": f"sut.root does not resolve to an existing directory: {sut_root}",
                "actions": [
                    "Set `sut.root` in `config/agentic-os.yml` to a valid path.",
                ],
            })
            return {"ok": False, "checks": checks}

        checks.append({
            "id": "sut_root",
            "status": "pass",
            "message": f"sut.root resolves: {sut_root}",
            "actions": [],
        })

        try:
            discovery = discover_sut(sut_root)
        except (OSError, ValueError) as exc:
            checks.append({
                "id": "sut_discovery",
                "status": "fail",
                "message": f"discovery failed: {exc}",
                "actions": ["Inspect `sut.root` content and re-run."],
            })
            return {"ok": False, "checks": checks}

        marker_count = sum(len(v) for v in (discovery.markers or {}).values())
        if discovery.stack == "unknown" and marker_count == 0:
            checks.append({
                "id": "sut_stack",
                "status": "fail",
                "message": (
                    f"stack=unknown and zero stack markers under {sut_root}. "
                    "Autonomy cannot pick a default test runner."
                ),
                "actions": [
                    "Point `sut.root` at the SUT directory (must contain `package.json` or `pyproject.toml`).",
                    "If running in online mode against a remote URL, set `web.enabled`/`api.enabled` and provide a real `test_runner` script.",
                ],
            })
        else:
            checks.append({
                "id": "sut_stack",
                "status": "pass",
                "message": f"stack={discovery.stack} markers={marker_count} tests={len(discovery.tests)}",
                "actions": [],
            })

    test_runner = sut_cfg.get("test_runner")
    if not test_runner:
        checks.append({
            "id": "test_runner",
            "status": "warn",
            "message": "sut.test_runner is empty — `run` workflows will no-op",
            "actions": [
                "Set `sut.test_runner` to a shell command (e.g. `./run-tests.sh`).",
            ],
        })
    else:
        runner_path = (paths.repo_root / test_runner).resolve() if not test_runner.startswith("/") else Path(test_runner)
        if not runner_path.exists():
            checks.append({
                "id": "test_runner",
                "status": "warn",
                "message": f"sut.test_runner points to a path that does not exist: {runner_path}",
                "actions": [
                    "Create the runner script or update `sut.test_runner` to the correct path.",
                ],
            })
        else:
            checks.append({
                "id": "test_runner",
                "status": "pass",
                "message": f"test_runner resolves: {runner_path}",
                "actions": [],
            })

    ok = all(c["status"] == "pass" for c in checks)
    return {"ok": ok, "checks": checks}
