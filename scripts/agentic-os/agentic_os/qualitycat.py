"""QualityCat integration — Python facade over the contest shell scripts.

Wraps the canonical scripts under `scripts/`. All commands go through
`runtime.subprocess.run_command` so timeouts, log capture, and process
group cleanup are uniform across the OS.

Public entry points:
    - file_bug(...)          → creates bugs/BUG-NNN-<slug>.md + evidence dir
    - reindex_bugs(...)      → rebuilds bugs/README.md from BUG-*.md
    - copy_reports(...)      → adapts build/ outputs into reports/
    - extract_last_run(...)  → writes reports/last-run.json from build outputs
    - build_summary(...)     → writes reports/summary.md from JSON + bugs
    - coverage_matrix(...)   → prints cucumber tag coverage matrix
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .atomic_io import atomic_write_json

from .errors import InfraError, UsageError
from .events import EventLog
from .ids import ulid
from .paths import RuntimePaths
from .runtime.subprocess import CommandResult, run_command
from .storage.db import transaction
from .time_utils import now_iso

SCRIPTS_DIR_REL = "scripts"
DEFAULT_TIMEOUT_SECONDS = 120
_BUG_FILENAME_RE = re.compile(r"^BUG-(\d{3})-[a-z0-9-]+\.md$")
_VALID_SEVERITIES = ("P0", "P1", "P2", "P3")


@dataclass(frozen=True)
class BugFiled:
    bug_id: str               # e.g. "BUG-001-negative-quantity-accepted"
    number: int               # 1
    severity: str             # "P0" .. "P3"
    bug_md_path: Path
    evidence_dir: Path
    command_result: CommandResult


def _script_path(paths: RuntimePaths, script_name: str) -> Path:
    candidate = paths.repo_root / SCRIPTS_DIR_REL / script_name
    if not candidate.exists():
        raise InfraError(
            f"qualitycat helper script missing: {candidate.relative_to(paths.repo_root)}"
        )
    return candidate


def _run_script(
    paths: RuntimePaths,
    events: EventLog,
    script: str,
    args: Sequence[str],
    *,
    kind: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    env: Optional[Mapping[str, str]] = None,
) -> CommandResult:
    script_path = _script_path(paths, script)
    log_path = paths.subprocess_logs_dir / f"{kind}-{ulid()}.log"
    command = ["/bin/bash", str(script_path), *args]
    result = run_command(
        command,
        cwd=paths.repo_root,
        log_path=log_path,
        timeout_seconds=timeout_seconds,
        env=env,
    )
    events.write(
        "qualitycat.script_run",
        severity="info" if result.exit_code == 0 else "warning",
        payload={
            "script": script,
            "kind": kind,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "log_path": str(log_path.relative_to(paths.repo_root)),
        },
    )
    return result


def _slugify(title: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", title.lower())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned or "untitled"


def _next_bug_number(bugs_dir: Path) -> int:
    if not bugs_dir.exists():
        return 1
    highest = 0
    for entry in bugs_dir.iterdir():
        m = _BUG_FILENAME_RE.match(entry.name)
        if m:
            highest = max(highest, int(m.group(1)))
    return highest + 1


_SEVERITY_LABEL = {
    "P0": "Critical",
    "P1": "High",
    "P2": "Medium",
    "P3": "Low",
}


def _hydrate_bug_markdown(
    bug_md_path: Path,
    *,
    bug_id: str,
    severity: str,
    scenario_tag: str,
    test_id: Optional[str],
    expected: Optional[str],
    actual: Optional[str],
    error_message: Optional[str],
    repro_command: Optional[str],
    spec_source: Optional[str],
    evidence_rel_paths: Sequence[str],
    auto_classified: bool = False,
) -> None:
    """Issue #85 — rewrite TBD placeholders in the freshly scaffolded
    bug file with concrete triage data so the file is usable as a real
    QA handoff instead of a skeleton.
    """
    text = bug_md_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    severity_label = _SEVERITY_LABEL.get(severity, severity)

    in_frontmatter = False
    frontmatter_seen = 0
    out: List[str] = []
    overrides = {
        "severity": severity_label,
        "scenario": scenario_tag,
        "found_by": (
            "triager-autopilot" if auto_classified else "agentic-os auto-triage"
        ),
    }
    if test_id:
        overrides["test"] = test_id
    extra_keys_seen: set[str] = set()
    for line in lines:
        if line.strip() == "---":
            frontmatter_seen += 1
            in_frontmatter = frontmatter_seen == 1
            # Issue #232 — inject `auto_classified: true` once on the
            # opening fence so the dashboard chip can surface autonomous
            # triage decisions for fast operator audit.
            if auto_classified and frontmatter_seen == 1:
                out.append(line)
                out.append("auto_classified: true")
                continue
            out.append(line)
            if frontmatter_seen >= 2:
                in_frontmatter = False
            continue
        if in_frontmatter:
            m = re.match(r"^([a-zA-Z_]+)\s*:\s*(.*)$", line)
            if m and m.group(1) in overrides:
                out.append(f"{m.group(1)}: {overrides[m.group(1)]}")
                continue
            if m and m.group(1) == "auto_classified" and auto_classified:
                # Skip duplicate if the skeleton already had the key.
                continue
        out.append(line)

    body = "\n".join(out)
    if not body.endswith("\n"):
        body += "\n"

    def _replace_section(text: str, header: str, replacement: str) -> str:
        pattern = re.compile(
            r"(##\s+" + re.escape(header) + r"[^\n]*\n)(.*?)(?=^##\s|\Z)",
            re.MULTILINE | re.DOTALL,
        )

        def _sub(match: re.Match) -> str:
            return match.group(1) + replacement

        new_text, n = pattern.subn(_sub, text, count=1)
        return new_text if n else text

    repro_block = (
        "1. Reproduce by running the scenario with its tag(s):\n\n"
        "```bash\n"
        f"{repro_command or './run-tests.sh'}\n"
        "```\n"
        f"\nScenario tag: `{scenario_tag}`"
        + (f"\nTest id: `{test_id}`\n\n" if test_id else "\n\n")
    )
    expected_block = (
        (f"Spec source: {spec_source}\n\n" if spec_source else "")
        + "```\n"
        + (expected or "Scenario must satisfy its asserted contract.")
        + "\n```\n\n"
    )
    actual_lines = []
    if error_message:
        actual_lines.append(error_message)
    if actual and actual != error_message:
        actual_lines.append(actual)
    actual_body = "\n".join(actual_lines) or "(no triage detail captured)"
    actual_block = "```\n" + actual_body + "\n```\n\n"

    if evidence_rel_paths:
        evidence_block = (
            "\n".join(f"- `{rel}`" for rel in evidence_rel_paths) + "\n\n"
        )
    else:
        evidence_block = f"- `evidence/{bug_id}/` (auto-created, no captured files)\n\n"

    body = _replace_section(body, "Steps to Reproduce", repro_block)
    body = _replace_section(body, "Expected", expected_block)
    body = _replace_section(body, "Actual", actual_block)
    body = _replace_section(body, "Evidence", evidence_block)

    bug_md_path.write_text(body, encoding="utf-8")


def file_bug(
    *,
    paths: RuntimePaths,
    events: EventLog,
    conn: sqlite3.Connection,
    title: str,
    severity: str,
    scenario_tag: str,
    evidence_files: Optional[Iterable[Path]] = None,
    test_id: Optional[str] = None,
    expected: Optional[str] = None,
    actual: Optional[str] = None,
    error_message: Optional[str] = None,
    repro_command: Optional[str] = None,
    spec_source: Optional[str] = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    auto_classified: bool = False,
) -> BugFiled:
    """Run `scripts/new-bug.sh "<title>"` and record the row in `bugs`.

    The shell script is responsible for the markdown skeleton + index
    update. This wrapper computes the bug id deterministically, copies
    evidence files into the runtime evidence dir, hydrates the
    Markdown skeleton with concrete triage fields (issue #85), and
    inserts a `bugs` row inside a single transaction.
    """
    if severity not in _VALID_SEVERITIES:
        raise UsageError(f"severity must be one of {_VALID_SEVERITIES}, got {severity!r}")
    if not title.strip():
        raise UsageError("bug title must be non-empty")
    if not scenario_tag.strip():
        raise UsageError("scenario_tag must be non-empty")

    bugs_dir = paths.repo_root / "bugs"
    number = _next_bug_number(bugs_dir)
    slug = _slugify(title)
    bug_id = f"BUG-{number:03d}-{slug}"

    result = _run_script(
        paths,
        events,
        "new-bug.sh",
        [title],
        kind="bug-file",
        timeout_seconds=timeout_seconds,
    )
    if result.exit_code != 0:
        raise InfraError(
            f"new-bug.sh failed (exit={result.exit_code}); see {result.log_path}"
        )

    bug_md_path = bugs_dir / f"{bug_id}.md"
    if not bug_md_path.exists():
        # The script may have used a different slug rule; locate by number.
        for entry in bugs_dir.iterdir():
            m = _BUG_FILENAME_RE.match(entry.name)
            if m and int(m.group(1)) == number:
                bug_md_path = entry
                bug_id = entry.stem
                break
    if not bug_md_path.exists():
        raise InfraError(f"new-bug.sh did not produce a markdown file for {bug_id}")

    evidence_dir = paths.evidence_dir / bug_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    copied_evidence: List[str] = []
    for src in evidence_files or ():
        src_path = Path(src)
        if not src_path.exists():
            continue
        shutil.copy2(src_path, evidence_dir / src_path.name)
        copied_evidence.append(
            str((evidence_dir / src_path.name).relative_to(paths.repo_root))
        )

    # Issue #85 — replace TBD placeholders with real triage data.
    try:
        _hydrate_bug_markdown(
            bug_md_path,
            bug_id=bug_id,
            severity=severity,
            scenario_tag=scenario_tag,
            test_id=test_id,
            expected=expected,
            actual=actual,
            error_message=error_message,
            repro_command=repro_command,
            spec_source=spec_source,
            evidence_rel_paths=copied_evidence,
            auto_classified=auto_classified,
        )
    except Exception as exc:  # hydration must not block bug-filing
        events.write(
            "bug.hydration_failed",
            severity="warning",
            payload={"bug_id": bug_id, "error": str(exc)},
        )
    else:
        # Reindex picks up the new severity label / scenario.
        try:
            reindex_bugs(paths, events)
        except Exception as exc:
            events.write(
                "bug.reindex_failed",
                severity="warning",
                payload={"bug_id": bug_id, "error": str(exc)},
            )

    ts = now_iso()
    db_id = ulid()
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO bugs(id, scenario_tag, severity, status, evidence_dir,
                             first_seen, last_seen)
            VALUES (?, ?, ?, 'open', ?, ?, ?);
            """,
            (
                db_id,
                scenario_tag,
                severity,
                str(evidence_dir.relative_to(paths.repo_root)),
                ts,
                ts,
            ),
        )
    events.write(
        "bug.filed",
        severity="warning",
        payload={
            "bug_id": bug_id,
            "db_id": db_id,
            "severity": severity,
            "scenario_tag": scenario_tag,
            "evidence_dir": str(evidence_dir.relative_to(paths.repo_root)),
            "bug_md_path": str(bug_md_path.relative_to(paths.repo_root)),
        },
    )
    return BugFiled(
        bug_id=bug_id,
        number=number,
        severity=severity,
        bug_md_path=bug_md_path,
        evidence_dir=evidence_dir,
        command_result=result,
    )


def reindex_bugs(paths: RuntimePaths, events: EventLog) -> CommandResult:
    return _run_script(paths, events, "new-bug.sh", ["--reindex"], kind="bug-reindex")


def copy_reports(
    paths: RuntimePaths, events: EventLog, *, clean: bool = False, timeout_seconds: int = 300
) -> CommandResult:
    args: List[str] = ["--clean"] if clean else []
    return _run_script(
        paths, events, "copy-reports.sh", args, kind="reports-copy",
        timeout_seconds=timeout_seconds,
    )


def extract_last_run(paths: RuntimePaths, events: EventLog) -> Dict[str, Any]:
    result = _run_script(paths, events, "extract-last-run.sh", [], kind="reports-extract")
    if result.exit_code != 0:
        raise InfraError(
            f"extract-last-run.sh failed (exit={result.exit_code}); see {result.log_path}"
        )
    target = paths.repo_root / "reports" / "last-run.json"
    if not target.exists():
        raise InfraError("extract-last-run.sh did not write reports/last-run.json")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InfraError(f"reports/last-run.json is not valid JSON: {exc}") from exc
    # Issue #99 — merge Playwright JSON report if present so triage and
    # the run_report final-gate pillar see generated-test failures too.
    pw_report = paths.repo_root / "reports" / "playwright" / "report.json"
    if pw_report.is_file():
        try:
            data = _merge_playwright_report(data, pw_report)
            atomic_write_json(target, data)
        except Exception as exc:
            events.write(
                "reports.playwright_merge_failed",
                severity="warning",
                payload={"error": str(exc)},
            )
    return data


def _merge_playwright_report(base: Dict[str, Any], pw_report_path: Path) -> Dict[str, Any]:
    """Parse Playwright JSON reporter output and merge totals + failures
    into the existing last-run.json shape (issue #99).

    Playwright's JSON reporter writes a `stats` block and per-suite
    `specs`/`tests` arrays. The schema is stable enough to extract
    pass/fail counts and failure scenarios without taking on a full
    Playwright dependency.
    """
    pw = json.loads(pw_report_path.read_text(encoding="utf-8"))
    stats = pw.get("stats") or {}
    expected = int(stats.get("expected") or 0)
    unexpected = int(stats.get("unexpected") or 0)
    flaky = int(stats.get("flaky") or 0)
    skipped = int(stats.get("skipped") or 0)
    pw_total = expected + unexpected + flaky + skipped
    pw_failed = unexpected + flaky

    failures = list(base.get("failures") or [])

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for spec in node.get("specs") or []:
                for test in spec.get("tests") or []:
                    results = test.get("results") or []
                    last = results[-1] if results else {}
                    status = (last.get("status") or "").lower()
                    if status in {"failed", "timedout", "interrupted"}:
                        title = (spec.get("title") or test.get("title") or "").strip()
                        errors = last.get("error") or {}
                        attachments = last.get("attachments") or []
                        screenshot = next(
                            (a.get("path") for a in attachments if a.get("name") == "screenshot"),
                            None,
                        )
                        trace = next(
                            (a.get("path") for a in attachments if a.get("name") == "trace"),
                            None,
                        )
                        failures.append(
                            {
                                "scenario": title,
                                "classname": spec.get("file") or "",
                                "tags": [],
                                "error_message": errors.get("message") or "",
                                "stack_head": errors.get("stack") or "",
                                "screenshot": screenshot,
                                "trace": trace,
                                "runner": "playwright",
                            }
                        )
            for child in node.get("suites") or []:
                _walk(child)

    for top in pw.get("suites") or []:
        _walk(top)

    base["failures"] = failures
    # Sum, preserving any counts already in the JUnit-derived base.
    base["total"] = int(base.get("total") or 0) + pw_total
    base["passed"] = int(base.get("passed") or 0) + expected
    base["failed"] = int(base.get("failed") or 0) + pw_failed
    base["skipped"] = int(base.get("skipped") or 0) + skipped
    return base


def build_summary(paths: RuntimePaths, events: EventLog) -> CommandResult:
    return _run_script(paths, events, "build-summary.sh", [], kind="reports-summary")


def coverage_matrix(
    paths: RuntimePaths, events: EventLog, features_dir: Optional[Path] = None
) -> CommandResult:
    args: List[str] = []
    if features_dir is not None:
        args.append(str(features_dir))
    return _run_script(
        paths, events, "coverage-matrix.sh", args, kind="reports-coverage", timeout_seconds=60
    )


def utc_now() -> str:
    """Helper for callers writing manifests outside event log scope."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
