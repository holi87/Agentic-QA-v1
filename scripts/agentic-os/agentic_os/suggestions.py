"""suggestions engine (heurystyki dashboard widget)."""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from .paths import RuntimePaths


def compute_suggestions(paths: RuntimePaths, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Zwraca uporządkowaną listę sugestii operatora."""
    out: List[Dict[str, Any]] = []

    # 1. Tasks bez analizy (status='queued' bez artifact kind='analysis').
    try:
        rows = conn.execute(
            """
            SELECT w.id, w.title FROM work_items w
            WHERE w.status='queued'
              AND NOT EXISTS (
                SELECT 1 FROM work_item_artifacts a
                 WHERE a.work_item_id = w.id AND a.kind = 'analysis'
              )
            LIMIT 5;
            """
        ).fetchall()
        if rows:
            out.append(
                {
                    "kind": "run_analyze",
                    "priority": "P1",
                    "message": f"{len(rows)} task(s) without analyze",
                    "targets": [{"id": r["id"], "title": r["title"]} for r in rows],
                    "action": {"endpoint": "/api/tasks/<id>/analyze", "label": "Analyze"},
                }
            )
    except sqlite3.Error:
        pass

    # 2. Tasks z planem ale bez patches.
    try:
        rows = conn.execute(
            """
            SELECT w.id, w.title FROM work_items w
            WHERE w.status = 'planned'
              AND EXISTS (
                SELECT 1 FROM work_item_artifacts a
                 WHERE a.work_item_id = w.id AND a.kind = 'test_plan'
              )
              AND NOT EXISTS (
                SELECT 1 FROM work_item_artifacts a
                 WHERE a.work_item_id = w.id AND a.kind = 'patch'
              )
            LIMIT 5;
            """
        ).fetchall()
        if rows:
            out.append(
                {
                    "kind": "generate_tests",
                    "priority": "P1",
                    "message": f"{len(rows)} planned task(s) waiting for tests",
                    "targets": [{"id": r["id"], "title": r["title"]} for r in rows],
                    "action": {"endpoint": "/api/tasks/<id>/implement-tests", "label": "Generate tests"},
                }
            )
    except sqlite3.Error:
        pass

    # 3. Patches waiting (z gates.describe_blocking_patches).
    try:
        from .gates import describe_blocking_patches

        patches = describe_blocking_patches(paths, conn=conn)
        waiting = [p for p in patches if p.get("state") == "waiting"]
        if waiting:
            out.append(
                {
                    "kind": "review_pending_patches",
                    "priority": "P0",
                    "message": f"{len(waiting)} patch(es) waiting for review",
                    "targets": [{"id": p["work_item_id"], "patch_path": p["patch_path"]} for p in waiting[:5]],
                    "action": {"endpoint": "/api/tasks/<id>/review-gate", "label": "Review gate"},
                }
            )
    except Exception:
        pass

    # 4. Tasks z run ale bez bug DB row dla failed.
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT w.id, w.title FROM work_items w
            JOIN work_item_artifacts a ON a.work_item_id = w.id
            WHERE a.kind = 'run'
              AND w.status IN ('running', 'bug_adjudication')
            LIMIT 5;
            """
        ).fetchall()
        if rows:
            out.append(
                {
                    "kind": "adjudicate_failures",
                    "priority": "P1",
                    "message": f"{len(rows)} task(s) need bug adjudication",
                    "targets": [{"id": r["id"], "title": r["title"]} for r in rows],
                    "action": {"endpoint": None, "label": "Open task detail"},
                }
            )
    except sqlite3.Error:
        pass

    return out
