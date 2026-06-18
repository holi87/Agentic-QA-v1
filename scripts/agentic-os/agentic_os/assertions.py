"""Assertion-change guard.

The guard is intentionally conservative: removing assertion-like lines is a
weakening, and ambiguous assertion rewrites require a recorded decision before
the change can pass.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .ids import ulid
from .orchestrator import CURRENT_PHASE_ID, SEEDED_PHASES
from .storage import init_db
from .storage.db import transaction
from .time_utils import now_iso

ASSERTION_RE = re.compile(
    r"\b(assert|assertThat|assertEquals|expect|should|must|toBe|toEqual|toContain|statusCode|Then)\b",
    re.IGNORECASE,
)
WEAK_TOKENS = (
    "contains",
    "tocontain",
    "matches",
    "notnull",
    "notempty",
    "exists",
    "optional",
    "maybe",
    "approximately",
    "greaterorequal",
)
EXACT_TOKENS = (
    "==",
    "===",
    "toeql",
    "toequal",
    "tobe",
    "equals",
    "assertequals",
    "statuscode",
)


@dataclass(frozen=True)
class AssertionChange:
    file_path: str
    assertion_before: str
    assertion_after: str
    classification: str
    reason: str


@dataclass(frozen=True)
class AssertionGuardResult:
    ok: bool
    changes: list[AssertionChange]
    blocked: int
    needs_decision: int

    def to_json(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "blocked": self.blocked,
            "needs_decision": self.needs_decision,
            "changes": [
                {
                    "file_path": change.file_path,
                    "assertion_before": change.assertion_before,
                    "assertion_after": change.assertion_after,
                    "classification": change.classification,
                    "reason": change.reason,
                }
                for change in self.changes
            ],
        }


def guard_files(
    *,
    before_path: Path,
    after_path: Path,
    file_path: Optional[str] = None,
    decision_id: Optional[str] = None,
    db_path: Optional[Path] = None,
    task_id: Optional[str] = None,
    run_id: Optional[str] = None,
) -> AssertionGuardResult:
    before = before_path.read_text(encoding="utf-8")
    after = after_path.read_text(encoding="utf-8")
    result = analyze_assertion_change(
        before,
        after,
        file_path=file_path or str(after_path),
        decision_id=decision_id,
    )
    if db_path is not None and result.changes:
        record_assertion_changes(
            db_path=db_path,
            changes=result.changes,
            decision_id=decision_id,
            task_id=task_id,
            run_id=run_id,
        )
    return result


def analyze_assertion_change(
    before: str,
    after: str,
    *,
    file_path: str,
    decision_id: Optional[str] = None,
) -> AssertionGuardResult:
    before_assertions = list(_assertion_lines(before))
    after_assertions = list(_assertion_lines(after))
    changes: list[AssertionChange] = []

    matcher = difflib.SequenceMatcher(a=before_assertions, b=after_assertions, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed = before_assertions[i1:i2]
        added = after_assertions[j1:j2]
        if tag == "delete":
            for line in removed:
                changes.append(
                    AssertionChange(file_path, line, "", "weakened", "assertion removed")
                )
            continue
        if tag == "insert":
            for line in added:
                changes.append(
                    AssertionChange(file_path, "", line, "strengthened", "assertion added")
                )
            continue
        changes.extend(_classify_replacements(file_path, removed, added))

    # Issue #367 — beyond assertion weakening, statically reject Playwright+TS
    # anti-patterns the patch INTRODUCES (lines new to `after`): hard waits (§5),
    # hardcoded URLs and hardcoded secrets (§8). Env-injected output (the
    # generators' own `process.env[...]` URLs and `Bearer ${process.env[...]}`)
    # is never flagged.
    changes.extend(_static_violations(before, after, file_path))

    blocked = 0
    needs_decision = 0
    for change in changes:
        if decision_id:
            continue
        if change.classification == "weakened":
            blocked += 1
        elif change.classification == "unknown":
            needs_decision += 1
    return AssertionGuardResult(
        ok=blocked == 0 and needs_decision == 0,
        changes=changes,
        blocked=blocked,
        needs_decision=needs_decision,
    )


def record_assertion_changes(
    *,
    db_path: Path,
    changes: Iterable[AssertionChange],
    decision_id: Optional[str] = None,
    task_id: Optional[str] = None,
    run_id: Optional[str] = None,
) -> None:
    conn = init_db(db_path)
    try:
        ts = now_iso()
        with transaction(conn):
            _ensure_current_phase(conn, ts)
            for change in changes:
                status = _status_for(change.classification, decision_id)
                conn.execute(
                    """
                    INSERT INTO assertion_changes(
                        id, task_id, run_id, file_path, assertion_before, assertion_after,
                        classification, decision_id, status, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        ulid(),
                        task_id,
                        run_id,
                        change.file_path,
                        change.assertion_before,
                        change.assertion_after,
                        change.classification,
                        decision_id,
                        status,
                        ts,
                    ),
                )
                if status in {"blocked", "needs_decision"}:
                    conn.execute(
                        """
                        INSERT INTO blockers(id, phase_id, severity, source, description, status, opened_at)
                        VALUES (?, ?, ?, 'assertion-guard', ?, 'open', ?);
                        """,
                        (
                            ulid(),
                            CURRENT_PHASE_ID,
                            "P1" if status == "blocked" else "P2",
                            _blocker_description(change, status),
                            ts,
                        ),
                    )
    finally:
        conn.close()


def _ensure_current_phase(conn: sqlite3.Connection, ts: str) -> None:
    branches = dict(SEEDED_PHASES)
    branch = branches.get(CURRENT_PHASE_ID, f"phase/{CURRENT_PHASE_ID}")
    conn.execute(
        """
        INSERT OR IGNORE INTO phases(id, status, branch, spec_path, updated_at)
        VALUES (?, 'planned', ?, ?, ?);
        """,
        (
            CURRENT_PHASE_ID,
            branch,
            f"docs/phases/{CURRENT_PHASE_ID}.md",
            ts,
        ),
    )


def _assertion_lines(text: str) -> Iterable[str]:
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith(("#", "//", "/*", "*")):
            continue
        if ASSERTION_RE.search(stripped):
            yield stripped


# Issue #367 — static Playwright+TS anti-pattern detectors.
HARD_WAIT_RE = re.compile(r"\.\s*waitForTimeout\s*\(")
URL_LITERAL_RE = re.compile(r"""['"`]https?://""", re.IGNORECASE)
SECRET_RE = re.compile(
    r"""(?ix)
      bearer\s+[A-Za-z0-9._\-]{8,}
    | (?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['"][^'"]{6,}['"]
    """
)


def _static_violations(before: str, after: str, file_path: str) -> list[AssertionChange]:
    """Flag Playwright+TS anti-patterns newly introduced in ``after``.

    Scans lines present in ``after`` but not in ``before`` (what the patch adds),
    skipping comments. Env-injected references (``process.env`` / ``os.environ``)
    are exempt from the URL/secret checks, so the generators' own output passes.
    """
    before_lines = {ln.strip() for ln in before.splitlines()}
    out: list[AssertionChange] = []
    for raw in after.splitlines():
        s = raw.strip()
        if not s or s.startswith(("#", "//", "/*", "*")):
            continue
        if s in before_lines:  # pre-existing — not introduced by this patch
            continue
        from_env = "process.env" in s or "os.environ" in s
        if HARD_WAIT_RE.search(s):
            out.append(AssertionChange(
                file_path, "", s, "weakened",
                "hard wait — wait on a condition, not the clock (standards §5)"))
        elif not from_env and URL_LITERAL_RE.search(s):
            out.append(AssertionChange(
                file_path, "", s, "weakened",
                "hardcoded URL — inject the base URL via env (standards §8)"))
        elif not from_env and SECRET_RE.search(s):
            out.append(AssertionChange(
                file_path, "", s, "weakened",
                "hardcoded secret — read credentials from process.env (standards §8)"))
    return out


def _classify_replacements(
    file_path: str,
    removed: list[str],
    added: list[str],
) -> list[AssertionChange]:
    changes: list[AssertionChange] = []
    used_added: set[int] = set()
    for before in removed:
        best_idx = _best_match(before, added, used_added)
        if best_idx is None:
            changes.append(
                AssertionChange(file_path, before, "", "weakened", "assertion removed")
            )
            continue
        used_added.add(best_idx)
        after = added[best_idx]
        classification, reason = _classify_pair(before, after)
        changes.append(AssertionChange(file_path, before, after, classification, reason))
    for idx, after in enumerate(added):
        if idx not in used_added:
            changes.append(AssertionChange(file_path, "", after, "strengthened", "assertion added"))
    return changes


def _best_match(before: str, added: list[str], used: set[int]) -> Optional[int]:
    best_idx: Optional[int] = None
    best_score = 0.0
    for idx, candidate in enumerate(added):
        if idx in used:
            continue
        score = difflib.SequenceMatcher(
            None, _normalize_assertion(before), _normalize_assertion(candidate)
        ).ratio()
        if score > best_score:
            best_idx = idx
            best_score = score
    return best_idx if best_score >= 0.35 else None


def _classify_pair(before: str, after: str) -> tuple[str, str]:
    if _normalize_assertion(before) == _normalize_assertion(after):
        return "unchanged", "normalized assertion unchanged"
    if _looks_weaker(before, after):
        return "weakened", "assertion became less exact"
    if _looks_strengthened(before, after):
        return "strengthened", "assertion became more exact"
    return "unknown", "assertion changed in a way that needs a decision"


def _looks_weaker(before: str, after: str) -> bool:
    b = _normalize_assertion(before)
    a = _normalize_assertion(after)
    if not a:
        return True
    if len(a) < max(12, int(len(b) * 0.65)):
        return True
    if "==" in b and (" in " in after or " in(" in a or "contains" in a):
        return True
    if _has_exact_token(b) and _has_weak_token(a) and not _has_weak_token(b):
        return True
    before_numbers = set(re.findall(r"\b\d{3}\b|\b\d+\b", before))
    after_numbers = set(re.findall(r"\b\d{3}\b|\b\d+\b", after))
    if before_numbers and not before_numbers.issubset(after_numbers):
        return True
    if "not " in b and "not " not in a:
        return True
    return False


def _looks_strengthened(before: str, after: str) -> bool:
    b = _normalize_assertion(before)
    a = _normalize_assertion(after)
    if _has_weak_token(b) and _has_exact_token(a):
        return True
    if len(a) > len(b) * 1.25 and not _has_weak_token(a):
        return True
    return False


def _normalize_assertion(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip().lower())


def _has_weak_token(text: str) -> bool:
    compact = re.sub(r"[^a-z0-9=]+", "", text)
    return any(token in compact or token in text for token in WEAK_TOKENS)


def _has_exact_token(text: str) -> bool:
    compact = re.sub(r"[^a-z0-9=]+", "", text)
    return any(token in compact or token in text for token in EXACT_TOKENS)


def _status_for(classification: str, decision_id: Optional[str]) -> str:
    if decision_id:
        return "allowed"
    if classification == "weakened":
        return "blocked"
    if classification == "unknown":
        return "needs_decision"
    return "allowed"


def _blocker_description(change: AssertionChange, status: str) -> str:
    payload = {
        "file_path": change.file_path,
        "classification": change.classification,
        "status": status,
        "reason": change.reason,
        "before": change.assertion_before,
        "after": change.assertion_after,
    }
    return "assertion change requires decision: " + json.dumps(payload, sort_keys=True)
