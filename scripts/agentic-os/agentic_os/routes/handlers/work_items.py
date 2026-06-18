"""WorkItemsMixin — extracted from routes/dashboard_server.py (issue #292)."""
from __future__ import annotations

import json
import contextvars
import hmac
import os
import secrets
import shutil
import sqlite3
import threading
import time
from html import escape as html_escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from ...config import ConfigError, load_or_default
from ...errors import UsageError
from ...events import EventLog, event_log_for_paths
from ...orchestrator import (
    CURRENT_PHASE_ID,
    Orchestrator,
    fetch_active_leases,
    fetch_bug_summary,
    fetch_last_run,
    fetch_phase_rows,
    fetch_task_summary,
    list_open_blockers,
)
from ...workflows import (
    WorkflowResult,
    run_final_gate,
    run_review_gate,
    run_tests,
)
from ...paths import RuntimePaths
from ...time_utils import now_iso
from ...storage.db import connect, integrity_check
from ...work_items import (
    annotate_spec_status,
    compute_candidate_debt,
    create_work_item_from_payload,
    delete_work_item,
    get_work_item,
    get_work_item_detail,
    list_work_item_artifacts,
    list_work_items,
    prune_orphan_work_items,
    read_work_item_spec,
    work_item_summary,
)
from ...dashboard import build_charts, build_overview, build_preflight
from ...analysis import analyze_work_item
from ...patch_builder import implement_tests_for_work_item
from ...security import redact_sensitive_text, resolve_repo_path
from ...runtime.tuning import MAX_JSON_BODY_BYTES as _MAX_JSON_BODY_BYTES
from .._dispatch import RouteDispatcher
from ...test_planning import (
    plan_work_item,
    read_plan_candidates,
    update_plan_candidate_decision,
)
from .._dashboard_state import (  # noqa: F401
    DEFAULT_HOST,
    DEFAULT_PORT,
    NAV_ACTIVE,
    NAV_LINKS,
    NAV_SENTINEL,
    STATIC_CONTENT_TYPES,
    STATIC_DIR,
    TEMPLATES_DIR,
    _ACTION_ORDER,
    _ALLOWED_LOCAL_HOSTS,
    _CONFIG_WRITE_DISABLED_MSG,
    _FULL_MODE_OVERRIDE,
    _FULL_MODE_OVERRIDE_EVENT,
    _ROUTES,
    _SSE_KEEPALIVE_SECONDS,
    _SSE_POLL_SECONDS,
    _WRITE_DISABLED_MSG,
    _autonomy_writes_active,
    _compute_action_gating,
    _content_type_header,
    _dashboard_config_write_settings,
    _dashboard_write_settings,
    _is_under,
    _load_or_create_dashboard_token,
    _open_db,
    _parse_json,
    _parse_kind_filter,
    _retention_sweep_on_startup,
    _workflow_payload,
    build_status,
    fetch_blocker_detail,
    fetch_coverage_state,
    fetch_task_detail,
    generated_tests_for_work_item,
    is_full_mode_active,
    render_nav,
    set_full_mode_override,
)



class WorkItemsMixin:
    """Methods grouped by domain; merged into ``_Handler`` via MRO."""

    _GENERATED_TESTS_ROOTS = ("tests/api", "tests/ui", "tests/generated")

    def _serve_task(self, task_id: str) -> None:
        if not task_id:
            self._send_404()
            return
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_404()
            return
        try:
            detail = fetch_task_detail(conn, task_id)
        finally:
            conn.close()
        if detail is None:
            self._send_404()
            return
        self._send_json(HTTPStatus.OK, detail)

    def _serve_work_items(self) -> None:
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.OK, {"tasks": [], "orphans": 0})
            return
        try:
            tasks = annotate_spec_status(self.paths, list_work_items(conn))
        finally:
            conn.close()
        orphans = sum(1 for t in tasks if t.get("spec_missing"))
        self._send_json(HTTPStatus.OK, {"tasks": tasks, "orphans": orphans})

    def _serve_work_item_path(self, suffix: str) -> None:
        if not suffix:
            self._send_404()
            return
        if suffix.endswith("/candidates"):
            work_item_id = suffix[: -len("/candidates")]
            self._serve_work_item_candidates(work_item_id)
            return
        if suffix.endswith("/artifacts"):
            work_item_id = suffix[: -len("/artifacts")]
            self._serve_work_item_artifacts(work_item_id)
            return
        if suffix.endswith("/spec"):
            work_item_id = suffix[: -len("/spec")]
            self._serve_work_item_spec(work_item_id)
            return
        if suffix.endswith("/gating"):
            work_item_id = suffix[: -len("/gating")]
            self._serve_work_item_gating(work_item_id)
            return
        # Issue #225 — generated tests review endpoints.
        if suffix.endswith("/generated-tests"):
            work_item_id = suffix[: -len("/generated-tests")]
            self._serve_generated_tests_list(work_item_id)
            return
        if "/generated-tests/" in suffix:
            wid, _, rel = suffix.partition("/generated-tests/")
            if wid and rel:
                self._serve_generated_test_file(wid, rel)
                return
        self._serve_work_item(suffix)

    def _serve_work_item_spec(self, work_item_id: str) -> None:
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_404()
            return
        try:
            work_item = get_work_item(conn, work_item_id)
        finally:
            conn.close()
        if work_item is None:
            self._send_404()
            return
        try:
            markdown = read_work_item_spec(self.paths, work_item)
        except UsageError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "spec_unavailable", "message": str(exc)})
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "work_item_id": work_item_id,
                "spec_path": work_item["spec_path"],
                "markdown": markdown,
            },
        )

    def _serve_work_item_candidates(self, work_item_id: str) -> None:
        try:
            result = read_plan_candidates(self.paths, work_item_id=work_item_id)
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        self._send_json(HTTPStatus.OK, result)

    def _serve_work_item_gating(self, work_item_id: str) -> None:
        """Issue #194 — surface per-action prerequisite state to the dashboard.

        Returns ``{"work_item_id": ..., "actions": {<kind>: {"enabled":
        bool, "reason": str}}}``. The shape is stable so the JS can grey
        out buttons and use the ``reason`` string as a tooltip without
        recomputing the predicate browser-side.
        """
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_404()
            return
        try:
            if get_work_item(conn, work_item_id) is None:
                self._send_404()
                return
            actions = _compute_action_gating(conn, self.paths, work_item_id)
        finally:
            conn.close()
        self._send_json(
            HTTPStatus.OK,
            {"work_item_id": work_item_id, "actions": actions},
        )

    def _candidate_decision_action(self, suffix: str) -> None:
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": "dashboard_write_disabled",
                    "message": _WRITE_DISABLED_MSG,
                },
            )
            return
        parts = [p for p in suffix.split("/") if p]
        # <work-item-id>/candidates/approve-all
        if len(parts) == 3 and parts[1] == "candidates" and parts[2] == "approve-all":
            self._candidate_approve_all_action(parts[0])
            return
        # <work-item-id>/candidates/<candidate-id>/<approve|reject|needs-decision>
        if len(parts) != 4 or parts[1] != "candidates":
            self._send_404()
            return
        work_item_id, _, candidate_id, action = parts
        decision_by_action = {
            "approve": "generate_now",
            "reject": "not_testable",
            "needs-decision": "needs_operator_decision",
        }
        if action not in decision_by_action:
            self._send_404()
            return
        try:
            body = self._read_optional_json_body()
            # Issue #79 — accept the full operator-facing decision form
            # from the dashboard so candidates can be approved with
            # required_test_data / cleanup / target / metadata without
            # falling back to CLI.
            lifecycle_tags = body.get("lifecycle_tags")
            if isinstance(lifecycle_tags, str):
                lifecycle_tags = [
                    t.strip() for t in lifecycle_tags.split(",") if t.strip()
                ]
            result = update_plan_candidate_decision(
                self.paths,
                work_item_id=work_item_id,
                candidate_id=candidate_id,
                decision=decision_by_action[action],
                expected_assertion=body.get("expected_assertion"),
                required_test_data=body.get("required_test_data") or body.get("test_data"),
                cleanup_strategy=body.get("cleanup_strategy"),
                target_page=body.get("target_page"),
                functional_area=body.get("functional_area"),
                lifecycle_tags=lifecycle_tags,
                reason=body.get("reason"),
            )
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        self._send_json(HTTPStatus.OK, result)

    def _candidate_approve_all_action(self, work_item_id: str) -> None:
        """Approve all currently reviewable API/UI candidates that validate.

        Wave 13 (#313 / RC gap 2) — shares the
        ``approve_all_runnable_candidates`` helper with the CLI so the
        dashboard and ``task approve-all-candidates`` never drift. The
        outcome shape (``approved`` / ``skipped`` / ``failed`` /
        ``outcomes`` / ``summary``) stays unchanged for existing
        consumers.
        """
        try:
            from ...test_planning import approve_all_runnable_candidates

            result = approve_all_runnable_candidates(
                self.paths,
                work_item_id=work_item_id,
                reason="dashboard approve all",
            )
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        self._send_json(HTTPStatus.OK, result)

    def _invoke_action(self, work_item_id: str, action: str) -> None:
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": "dashboard_write_disabled",
                    "message": _WRITE_DISABLED_MSG,
                },
            )
            return
        body: Dict[str, Any] = {}
        if action in {"review-gate", "apply-patch", "run-tests", "final-gate"}:
            try:
                body = self._read_optional_json_body()
            except UsageError as exc:
                self._send_usage_error(exc)
                return
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "db_missing"})
            return
        events = event_log_for_paths(conn, self.paths)
        # Issue #194 — gate by prerequisites before dispatching. The
        # dashboard UI greys out blocked actions but we still enforce
        # server-side so an out-of-date page or a direct API caller
        # cannot fire e.g. `final-gate` on a task with no run manifest.
        if action in _ACTION_ORDER:
            try:
                if get_work_item(conn, work_item_id) is None:
                    raise UsageError(f"unknown task id: {work_item_id}")
                gating = _compute_action_gating(conn, self.paths, work_item_id)
            except UsageError as exc:
                conn.close()
                self._send_usage_error(exc)
                return
            entry = gating.get(action)
            if entry is not None and not entry["enabled"]:
                conn.close()
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "error": "action_blocked",
                        "action": action,
                        "reason": entry["reason"],
                    },
                )
                return
        try:
            if action == "analyze":
                result = analyze_work_item(conn, self.paths, events, work_item_id=work_item_id)
            elif action == "plan":
                result = plan_work_item(conn, self.paths, events, work_item_id=work_item_id)
            elif action == "implement-tests":
                result = implement_tests_for_work_item(
                    conn, self.paths, events, work_item_id=work_item_id
                )
            elif action == "review-gate":
                result = self._run_review_gate_for_work_item(conn, events, work_item_id, body)
            elif action == "apply-patch":
                # Issue #80 — explicit dashboard apply step.
                result = self._apply_approved_patch_for_work_item(
                    conn, events, work_item_id, body
                )
            elif action == "run-tests":
                result = self._run_tests_for_work_item(conn, events, work_item_id, body)
            elif action == "final-gate":
                result = self._run_final_gate_for_work_item(conn, events, work_item_id)
            else:
                self._send_404()
                return
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        finally:
            conn.close()
        self._send_json(HTTPStatus.OK, result)

    def _run_review_gate_for_work_item(
        self,
        conn: sqlite3.Connection,
        events: EventLog,
        work_item_id: str,
        body: Dict[str, Any],
    ) -> Dict[str, Any]:
        from ...work_items import get_work_item, list_work_item_artifacts

        if get_work_item(conn, work_item_id) is None:
            raise UsageError(f"unknown task id: {work_item_id}")
        scope = str(body.get("scope") or "assertion")
        if scope not in {"api", "ui", "assertion", "final", "general"}:
            raise UsageError(f"invalid scope: {scope}")
        latest_patch_rel: Optional[str] = None
        for art in list_work_item_artifacts(conn, work_item_id):
            if art["kind"] == "patch":
                latest_patch_rel = art["path"]
        if latest_patch_rel is None:
            raise UsageError(
                "no patch artifact on this task — run Generate tests first"
            )
        latest_patch = self.paths.repo_root / latest_patch_rel
        if not latest_patch.exists():
            raise UsageError(f"patch missing on disk: {latest_patch_rel}")
        orch = Orchestrator(conn, self.paths, events)
        result = run_review_gate(
            orch,
            self.paths,
            events,
            diff_path=Path(latest_patch_rel),
            scope=scope,
            reviewer_output_path=None,
            apply_patch_path=None,
            work_item_id=work_item_id,
        )
        return _workflow_payload(result, extra={"scope": scope, "patch_path": latest_patch_rel})

    def _apply_approved_patch_for_work_item(
        self,
        conn: sqlite3.Connection,
        events: EventLog,
        work_item_id: str,
        body: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Issue #80 — explicit dashboard apply step.

        Runs the review gate with the same patch as `--diff` and
        `--apply-patch`, so identity-checked apply (issue #109) is
        enforced. The dashboard now completes the apply step without
        falling back to CLI.
        """
        from ...work_items import get_work_item, list_work_item_artifacts

        if get_work_item(conn, work_item_id) is None:
            raise UsageError(f"unknown task id: {work_item_id}")
        scope = str(body.get("scope") or "assertion")
        if scope not in {"api", "ui", "assertion", "final", "general"}:
            raise UsageError(f"invalid scope: {scope}")
        latest_patch_rel: Optional[str] = None
        for art in list_work_item_artifacts(conn, work_item_id):
            if art["kind"] == "patch":
                latest_patch_rel = art["path"]
        if latest_patch_rel is None:
            raise UsageError(
                "no patch artifact on this task — run Generate tests first"
            )
        latest_patch = self.paths.repo_root / latest_patch_rel
        if not latest_patch.exists():
            raise UsageError(f"patch missing on disk: {latest_patch_rel}")
        orch = Orchestrator(conn, self.paths, events)
        result = run_review_gate(
            orch,
            self.paths,
            events,
            diff_path=Path(latest_patch_rel),
            scope=scope,
            reviewer_output_path=None,
            apply_patch_path=Path(latest_patch_rel),
            work_item_id=work_item_id,
        )
        return _workflow_payload(
            result, extra={"scope": scope, "patch_path": latest_patch_rel, "applied": True}
        )

    def _run_tests_for_work_item(
        self,
        conn: sqlite3.Connection,
        events: EventLog,
        work_item_id: str,
        body: Dict[str, Any],
    ) -> Dict[str, Any]:
        from ...work_items import get_work_item

        if get_work_item(conn, work_item_id) is None:
            raise UsageError(f"unknown task id: {work_item_id}")
        tag = body.get("tag")
        if tag is not None and (not isinstance(tag, str) or not tag.strip()):
            raise UsageError("tag must be a non-empty string")
        orch = Orchestrator(conn, self.paths, events)
        result = run_tests(
            orch,
            self.paths,
            events,
            tag=tag.strip() if isinstance(tag, str) else None,
            work_item_id=work_item_id,
        )
        return _workflow_payload(result)

    def _run_final_gate_for_work_item(
        self,
        conn: sqlite3.Connection,
        events: EventLog,
        work_item_id: str,
    ) -> Dict[str, Any]:
        from ...work_items import get_work_item

        if get_work_item(conn, work_item_id) is None:
            raise UsageError(f"unknown task id: {work_item_id}")
        orch = Orchestrator(conn, self.paths, events)
        result = run_final_gate(orch, self.paths, events, work_item_id=work_item_id)
        return _workflow_payload(result)

    def _serve_work_item(self, work_item_id: str) -> None:
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_404()
            return
        try:
            detail = get_work_item_detail(conn, work_item_id)
        finally:
            conn.close()
        if detail is None:
            self._send_404()
            return
        # Issue #192 — surface candidate debt on the task detail too so
        # the dashboard task page can render a prominent plan summary
        # (AC #1) without re-reading TEST-PLAN.json browser-side.
        debt = compute_candidate_debt(self.paths, work_item_id)
        detail["candidate_debt"] = debt
        detail["done_with_pending_decisions"] = (
            (detail.get("work_item") or {}).get("status") == "done"
            and int(debt.get("needs_operator_decision", 0) or 0) > 0
        )
        detail["generated_tests"] = generated_tests_for_work_item(self.paths, work_item_id)
        # Issue #330 — surface the idempotent-no-op state derived from the
        # event log so the dashboard can show a "covered" banner instead of
        # leaving the operator staring at an unchanged status badge.
        conn2 = _open_db(self.paths)
        try:
            detail["coverage_state"] = fetch_coverage_state(conn2, work_item_id)
        finally:
            conn2.close()
        self._send_json(HTTPStatus.OK, detail)

    def _serve_work_item_artifacts(self, work_item_id: str) -> None:
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_404()
            return
        try:
            if get_work_item_detail(conn, work_item_id) is None:
                detail = None
            else:
                detail = {"artifacts": list_work_item_artifacts(conn, work_item_id)}
        finally:
            conn.close()
        if detail is None:
            self._send_404()
            return
        self._send_json(HTTPStatus.OK, detail)

    def _create_work_item(self) -> None:
        enabled, default_sut_root = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": "dashboard_write_disabled",
                    "message": _WRITE_DISABLED_MSG,
                },
            )
            return
        try:
            payload = self._read_json_body()
            conn = _open_db(self.paths)
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        except FileNotFoundError:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "db_missing"})
            return
        try:
            detail = create_work_item_from_payload(
                conn,
                self.paths,
                event_log_for_paths(conn, self.paths),
                payload,
                default_sut_root=default_sut_root,
            )
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        finally:
            conn.close()
        self._send_json(HTTPStatus.CREATED, detail)

    def _serve_patches(self, work_item_id: Optional[str]) -> None:
        """GET /api/patches[/<task-id>] — return blocking-patch summary."""
        from ...gates import describe_blocking_patches

        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.OK, {"patches": []})
            return
        try:
            patches = describe_blocking_patches(
                self.paths,
                conn=conn,
                work_item_id=work_item_id or None,
            )
        finally:
            conn.close()
        self._send_json(HTTPStatus.OK, {"patches": patches})

    def _delete_task_action(self, work_item_id: str) -> None:
        """DELETE /api/tasks/<id> — issue #224.

        Requires `dashboard.enable_write_endpoints=true`. Removes the work
        item, runtime artifacts and emits ``work_item.deleted``. The spec
        markdown file is left in place so operators can re-import it.
        """
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "dashboard_write_disabled", "message": _WRITE_DISABLED_MSG},
            )
            return
        try:
            body = self._read_optional_json_body()
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        reason = (body or {}).get("reason") if isinstance(body, dict) else None
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "db_missing"})
            return
        events = event_log_for_paths(conn, self.paths)
        try:
            result = delete_work_item(
                conn,
                self.paths,
                events,
                work_item_id=work_item_id,
                reason=str(reason) if reason else None,
            )
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        finally:
            conn.close()
        self._send_json(HTTPStatus.OK, {"ok": True, "result": result})

    def _collect_generated_tests(self, work_item_id: str) -> list[Dict[str, Any]]:
        """Return list of dicts with rel_path, exists, size, mtime, source
        for files emitted by the last `implement-tests` run.

        Strategy: parse the most recent ``.patch`` file under
        ``agentic-os-runtime/patches/<id>/`` and extract ``+++ b/<rel>``
        lines. Skip duplicates while preserving order.
        """
        patches_dir = self.paths.patches_dir / work_item_id
        if not patches_dir.exists():
            return []
        patch_files = sorted(
            (p for p in patches_dir.glob("*.patch") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not patch_files:
            return []
        latest = patch_files[0]
        try:
            text = latest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        seen: list[str] = []
        for line in text.splitlines():
            if line.startswith("+++ b/"):
                rel = line[len("+++ b/"):].strip()
                if rel and rel not in seen:
                    seen.append(rel)
        items: list[Dict[str, Any]] = []
        for rel in seen:
            abs_path = (self.paths.repo_root / rel).resolve()
            try:
                abs_path.relative_to(self.paths.repo_root.resolve())
            except ValueError:
                continue
            entry: Dict[str, Any] = {
                "relative_path": rel,
                "exists": abs_path.exists(),
            }
            if abs_path.exists() and abs_path.is_file():
                try:
                    stat = abs_path.stat()
                    entry["size"] = stat.st_size
                    entry["mtime"] = stat.st_mtime
                except OSError:
                    pass
            items.append(entry)
        return items

    def _serve_generated_tests_list(self, work_item_id: str) -> None:
        if not work_item_id:
            self._send_404()
            return
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_404()
            return
        try:
            work_item = get_work_item(conn, work_item_id)
        finally:
            conn.close()
        if work_item is None:
            self._send_404()
            return
        items = self._collect_generated_tests(work_item_id)
        self._send_json(
            HTTPStatus.OK,
            {"work_item_id": work_item_id, "items": items},
        )

    def _resolve_generated_test_path(
        self, work_item_id: str, rel: str
    ) -> Optional[Path]:
        if not rel or ".." in rel.split("/"):
            return None
        # Limit edits to known test roots so this endpoint can't be used to
        # read or write arbitrary files in the repository.
        if not any(rel == root or rel.startswith(root + "/") for root in self._GENERATED_TESTS_ROOTS):
            return None
        candidates = self._collect_generated_tests(work_item_id)
        if not any(c["relative_path"] == rel for c in candidates):
            return None
        target = (self.paths.repo_root / rel).resolve()
        try:
            target.relative_to(self.paths.repo_root.resolve())
        except ValueError:
            return None
        return target

    def _serve_generated_test_file(self, work_item_id: str, rel: str) -> None:
        target = self._resolve_generated_test_path(work_item_id, rel)
        if target is None:
            self._send_404()
            return
        if not target.exists() or not target.is_file():
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "test_file_missing", "relative_path": rel},
            )
            return
        try:
            content = target.read_text(encoding="utf-8")
        except OSError as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "read_failed", "message": str(exc)},
            )
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "work_item_id": work_item_id,
                "relative_path": rel,
                "content": content,
                "size": len(content),
            },
        )

    def _save_generated_test_action(self, work_item_id: str, rel: str) -> None:
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "dashboard_write_disabled", "message": _WRITE_DISABLED_MSG},
            )
            return
        target = self._resolve_generated_test_path(work_item_id, rel)
        if target is None:
            self._send_404()
            return
        try:
            body = self._read_optional_json_body()
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        if not isinstance(body, dict) or "content" not in body:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "missing_content", "message": "JSON body must include `content`"},
            )
            return
        content = body.get("content")
        if not isinstance(content, str):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_content", "message": "`content` must be a string"},
            )
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "write_failed", "message": str(exc)},
            )
            return
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            conn = None
        try:
            if conn is not None:
                events = event_log_for_paths(conn, self.paths)
                events.write(
                    "work_item.test_file_edited",
                    actor="operator",
                    payload={
                        "work_item_id": work_item_id,
                        "relative_path": rel,
                        "size": len(content),
                    },
                )
        finally:
            if conn is not None:
                conn.close()
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "work_item_id": work_item_id,
                "relative_path": rel,
                "size": len(content),
            },
        )

    def _abandon_patch_action(self, work_item_id: str) -> None:
        """POST /api/tasks/<id>/abandon-patch — operator abandons a patch."""
        from ...workflows import abandon_patch

        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "dashboard_write_disabled", "message": _WRITE_DISABLED_MSG},
            )
            return
        try:
            body = self._read_optional_json_body()
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        patch_path = (body or {}).get("patch_path")
        reason = (body or {}).get("reason")
        if not patch_path or not reason:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "missing_fields", "message": "patch_path and reason are required"},
            )
            return
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "db_missing"})
            return
        events = event_log_for_paths(conn, self.paths)
        try:
            result = abandon_patch(
                self.paths,
                events,
                task_id=work_item_id,
                patch_path=str(patch_path),
                reason=str(reason),
            )
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        finally:
            conn.close()
        self._send_json(HTTPStatus.OK, {"ok": True, "result": result})
