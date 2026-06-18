"""Shared dataclasses for the workflow stages (issue #292)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...atomic_io import atomic_write_json
from ...errors import UsageError
from ...events import EventLog
from ...gates import (
    GateFinding,
    GateResult,
    evaluate_final_gate,
    final_gate,
    merge_patch_if_approved,
    parse_gate_output,
    static_review_gate,
    write_abandon_artifact,
    write_gate_result,
)
from ...ids import ulid
from ...ids import run_id as new_run_id
from ...orchestrator import CURRENT_PHASE_ID, Orchestrator
from ...paths import RuntimePaths
from ...runtime.subprocess import run_command, scrub_provider_credentials
from ...security import resolve_repo_path
from ...storage.db import connect as _db_connect, transaction
from ...time_utils import now_iso



@dataclass
class WorkflowResult:
    ok: bool
    exit_code: int
    failure_kind: Optional[str]
    task_id: str
    run_id: str
    manifest_path: str
    reports_path: Optional[str]
    bugs_opened: List[str]

    def to_json(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "failure_kind": self.failure_kind,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "manifest_path": self.manifest_path,
            "reports_path": self.reports_path,
            "bugs_opened": self.bugs_opened,
        }
