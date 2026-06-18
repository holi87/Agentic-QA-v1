"""auto-git on SUT root + origin sync.

Wszystkie operacje gita uzywaja argv list (`["git", ...]`), nigdy shell
strings. Origin URL przepuszczony przez whitelist (ssh://, https://, lub
`git@host:owner/repo`). Path traversal odrzucany.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .errors import InfraError, UsageError
from .events import EventLog
from .paths import RuntimePaths
from .runtime.subprocess import run_command
from .time_utils import now_iso


_GIT_REMOTE_PATTERNS = (
    re.compile(r"^ssh://git@[A-Za-z0-9._-]+(?::\d+)?/[A-Za-z0-9._/-]+?(?:\.git)?$"),
    re.compile(r"^https://[A-Za-z0-9._-]+(?::\d+)?/[A-Za-z0-9._/-]+?(?:\.git)?$"),
    re.compile(r"^git@[A-Za-z0-9._-]+:[A-Za-z0-9._/-]+?(?:\.git)?$"),
)


# Issue #240 — per-process dedupe of `sut.git.skipped` emissions so we do
# not spam the events log on every poll when git is absent. One event per
# (op, sut_root) per process lifetime is sufficient — the dashboard reads
# the latest event anyway.
_SKIPPED_NOTIFIED: set[tuple[str, str]] = set()


def git_available() -> bool:
    """True when the `git` binary is on PATH. Cheap shutil.which probe."""
    return shutil.which("git") is not None


def _skipped_result() -> "GitOpResult":
    return GitOpResult(
        ok=False,
        exit_code=-1,
        detail={"skipped": True, "reason": "git_not_installed"},
    )


def _notify_skipped(events: Optional[EventLog], op: str, sut_root: str) -> None:
    if events is None:
        return
    key = (op, sut_root)
    if key in _SKIPPED_NOTIFIED:
        return
    _SKIPPED_NOTIFIED.add(key)
    events.write(
        "sut.git.skipped",
        severity="info",
        payload={"op": op, "sut_root": sut_root, "reason": "git_not_installed"},
    )


@dataclass(frozen=True)
class GitOpResult:
    ok: bool
    exit_code: int
    detail: Dict[str, object]


def validate_remote_url(url: str) -> None:
    """Whitelist git remote URLs."""
    if not isinstance(url, str) or not url:
        raise UsageError("remote URL must be a non-empty string")
    if not any(p.match(url) for p in _GIT_REMOTE_PATTERNS):
        raise UsageError(
            f"unsupported git remote URL: {url!r}; allowed: ssh://, https://, or git@host:owner/repo"
        )


def _safe_sut_root(repo_root: Path, sut_root: str) -> Path:
    """Resolve sut.root and refuse path traversal outside repo_root."""
    if not isinstance(sut_root, str) or not sut_root:
        raise UsageError("sut.root must be a non-empty string")
    candidate = (repo_root / sut_root).resolve()
    repo_resolved = repo_root.resolve()
    # Allow sut_root == repo itself or a subdir.
    try:
        candidate.relative_to(repo_resolved)
    except ValueError as exc:
        # Accept absolute sut_root only if explicitly outside repo_root AND
        # operator-configured. For now, refuse to be safe.
        raise UsageError(f"sut.root must resolve inside repo: {sut_root!r}") from exc
    return candidate


def has_git_repo(sut_path: Path) -> bool:
    return (sut_path / ".git").exists()


def git_init(
    paths: RuntimePaths,
    events: EventLog,
    *,
    sut_root: str,
) -> GitOpResult:
    """`git init` + initial commit jezeli repo jeszcze nie istnieje."""
    if not git_available():
        _notify_skipped(events, "init", sut_root)
        return _skipped_result()
    target = _safe_sut_root(paths.repo_root, sut_root)
    if not target.exists():
        raise UsageError(f"sut.root does not exist: {sut_root}")
    if has_git_repo(target):
        return GitOpResult(ok=True, exit_code=0, detail={"already_initialized": True})
    log = paths.subprocess_logs_dir / f"git-init-{now_iso().replace(':', '-')}.log"
    res = run_command(["git", "init"], cwd=target, log_path=log, timeout_seconds=30)
    if res.exit_code != 0:
        return GitOpResult(ok=False, exit_code=res.exit_code, detail={"step": "init"})
    res2 = run_command(["git", "add", "-A"], cwd=target, log_path=log, timeout_seconds=60)
    if res2.exit_code != 0:
        return GitOpResult(ok=False, exit_code=res2.exit_code, detail={"step": "add"})
    res3 = run_command(
        ["git", "-c", "user.email=agentic-os@local", "-c", "user.name=agentic-os",
         "commit", "-m", "agentic-os bootstrap", "--allow-empty"],
        cwd=target,
        log_path=log,
        timeout_seconds=30,
    )
    if res3.exit_code != 0:
        return GitOpResult(ok=False, exit_code=res3.exit_code, detail={"step": "commit"})
    events.write(
        "sut.git.init",
        payload={"sut_root": sut_root, "log": str(log.relative_to(paths.repo_root))},
    )
    return GitOpResult(ok=True, exit_code=0, detail={"initialized": True})


def git_set_remote(
    paths: RuntimePaths,
    events: EventLog,
    *,
    sut_root: str,
    remote_url: str,
    remote_name: str = "origin",
) -> GitOpResult:
    """Add or replace remote URL."""
    if not git_available():
        _notify_skipped(events, "set_remote", sut_root)
        return _skipped_result()
    validate_remote_url(remote_url)
    target = _safe_sut_root(paths.repo_root, sut_root)
    if not has_git_repo(target):
        raise UsageError(f"sut.root is not a git repo: {sut_root}")
    log = paths.subprocess_logs_dir / f"git-remote-{now_iso().replace(':', '-')}.log"
    # Try set-url first; if no existing remote, add it.
    res = run_command(
        ["git", "remote", "set-url", remote_name, remote_url],
        cwd=target,
        log_path=log,
        timeout_seconds=10,
    )
    if res.exit_code != 0:
        res = run_command(
            ["git", "remote", "add", remote_name, remote_url],
            cwd=target,
            log_path=log,
            timeout_seconds=10,
        )
        if res.exit_code != 0:
            return GitOpResult(ok=False, exit_code=res.exit_code, detail={"step": "add_remote"})
    events.write(
        "sut.git.remote_set",
        payload={"sut_root": sut_root, "remote_url": remote_url, "remote_name": remote_name},
    )
    return GitOpResult(ok=True, exit_code=0, detail={"remote_url": remote_url})


def git_publish_main(
    paths: RuntimePaths,
    events: EventLog,
    *,
    sut_root: str,
    remote_name: str = "origin",
) -> GitOpResult:
    """`git push -u origin main` jezeli origin pusty."""
    if not git_available():
        _notify_skipped(events, "publish_main", sut_root)
        return _skipped_result()
    target = _safe_sut_root(paths.repo_root, sut_root)
    if not has_git_repo(target):
        raise UsageError(f"sut.root is not a git repo: {sut_root}")
    log = paths.subprocess_logs_dir / f"git-publish-{now_iso().replace(':', '-')}.log"
    res = run_command(
        ["git", "push", "-u", remote_name, "main"],
        cwd=target,
        log_path=log,
        timeout_seconds=120,
    )
    if res.exit_code == 0:
        events.write("sut.git.publish", payload={"sut_root": sut_root, "remote_name": remote_name})
        return GitOpResult(ok=True, exit_code=0, detail={"pushed": True})
    return GitOpResult(ok=False, exit_code=res.exit_code, detail={"step": "push", "log": str(log.relative_to(paths.repo_root))})


def git_fetch(
    paths: RuntimePaths,
    events: EventLog,
    *,
    sut_root: str,
    remote_name: str = "origin",
) -> GitOpResult:
    """`git fetch <remote>`."""
    if not git_available():
        _notify_skipped(events, "fetch", sut_root)
        return _skipped_result()
    target = _safe_sut_root(paths.repo_root, sut_root)
    if not has_git_repo(target):
        raise UsageError(f"sut.root is not a git repo: {sut_root}")
    log = paths.subprocess_logs_dir / f"git-fetch-{now_iso().replace(':', '-')}.log"
    res = run_command(
        ["git", "fetch", remote_name],
        cwd=target,
        log_path=log,
        timeout_seconds=120,
    )
    ok = res.exit_code == 0
    if ok:
        events.write("sut.git.fetch", payload={"sut_root": sut_root, "remote_name": remote_name})
    return GitOpResult(ok=ok, exit_code=res.exit_code, detail={"log": str(log.relative_to(paths.repo_root))})


def git_pull_ff(
    paths: RuntimePaths,
    events: EventLog,
    *,
    sut_root: str,
    remote_name: str = "origin",
    branch: str = "main",
) -> GitOpResult:
    """`git pull --ff-only`; non-ff returns ok=False (no auto merge)."""
    if not git_available():
        _notify_skipped(events, "pull_ff", sut_root)
        return _skipped_result()
    target = _safe_sut_root(paths.repo_root, sut_root)
    if not has_git_repo(target):
        raise UsageError(f"sut.root is not a git repo: {sut_root}")
    log = paths.subprocess_logs_dir / f"git-pull-{now_iso().replace(':', '-')}.log"
    res = run_command(
        ["git", "pull", "--ff-only", remote_name, branch],
        cwd=target,
        log_path=log,
        timeout_seconds=120,
    )
    ok = res.exit_code == 0
    if ok:
        events.write("sut.git.pull", payload={"sut_root": sut_root, "branch": branch})
    else:
        events.write(
            "sut.git.pull_non_ff",
            severity="warning",
            payload={"sut_root": sut_root, "branch": branch, "log": str(log.relative_to(paths.repo_root))},
        )
    return GitOpResult(ok=ok, exit_code=res.exit_code, detail={"branch": branch})


_GIT_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
_WI_SLUG_PATTERN = re.compile(r"[^A-Za-z0-9-]+")


def _validate_branch_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise UsageError("branch name must be a non-empty string")
    if not _GIT_BRANCH_PATTERN.match(name):
        raise UsageError(f"branch name has unsupported characters: {name!r}")


def _slugify(value: str, *, max_length: int = 40) -> str:
    cleaned = _WI_SLUG_PATTERN.sub("-", (value or "").lower()).strip("-")
    return (cleaned or "wi")[:max_length] or "wi"


def work_item_branch_name(work_item_id: str, title: Optional[str] = None) -> str:
    """Return the canonical agentic-os branch name for a work item."""
    if not isinstance(work_item_id, str) or not work_item_id:
        raise UsageError("work_item_id must be a non-empty string")
    slug = _slugify(title or work_item_id)
    name = f"agentic-os/wi-{work_item_id}-{slug}"
    _validate_branch_name(name)
    return name


def _git_current_branch(target: Path, log: Path) -> Optional[str]:
    res = run_command(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=target,
        log_path=log,
        timeout_seconds=10,
    )
    if res.exit_code != 0:
        return None
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if line.startswith("[stdout] "):
                candidate = line[len("[stdout] "):].strip()
                if candidate:
                    return candidate
    except OSError:
        pass
    return None


def _git_working_tree_clean(target: Path, log: Path) -> bool:
    res = run_command(
        ["git", "status", "--porcelain"],
        cwd=target,
        log_path=log,
        timeout_seconds=10,
    )
    if res.exit_code != 0:
        return False
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        if line.startswith("[stdout] ") and line[len("[stdout] "):].strip():
            return False
    return True


def git_start_work_item_branch(
    paths: RuntimePaths,
    events: EventLog,
    *,
    sut_root: str,
    work_item_id: str,
    title: Optional[str] = None,
    base: str = "main",
) -> GitOpResult:
    """Create or switch to `agentic-os/wi-<id>-<slug>` from `base`.

    Issue #242. Idempotent: if branch exists, `git switch`. Refuses to
    branch off a dirty base — caller continues without branch context
    and `sut.git.branch_blocked` is emitted instead.
    """
    if not git_available():
        _notify_skipped(events, "wi_branch", sut_root)
        return _skipped_result()
    _validate_branch_name(base)
    target = _safe_sut_root(paths.repo_root, sut_root)
    if not has_git_repo(target):
        return GitOpResult(ok=False, exit_code=-1, detail={"reason": "not_a_repo"})

    branch = work_item_branch_name(work_item_id, title)
    log = paths.subprocess_logs_dir / f"git-wi-branch-{now_iso().replace(':', '-')}.log"
    current = _git_current_branch(target, log)

    # Already on the work-item branch — nothing to do.
    if current == branch:
        return GitOpResult(ok=True, exit_code=0, detail={"branch": branch, "switched": False})

    # Check the branch existence.
    list_res = run_command(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=target,
        log_path=log,
        timeout_seconds=10,
    )
    branch_exists = list_res.exit_code == 0

    if not branch_exists:
        # Only refuse base when we need to create the branch — switching
        # to an existing branch is safe even with WIP on disk.
        if not _git_working_tree_clean(target, log):
            events.write(
                "sut.git.branch_blocked",
                severity="warning",
                payload={
                    "work_item_id": work_item_id,
                    "branch": branch,
                    "reason": "dirty_working_tree",
                    "sut_root": sut_root,
                },
            )
            return GitOpResult(
                ok=False,
                exit_code=-1,
                detail={"reason": "dirty_working_tree", "branch": branch},
            )
        res = run_command(
            ["git", "switch", "-c", branch, base],
            cwd=target,
            log_path=log,
            timeout_seconds=15,
        )
        if res.exit_code != 0:
            # Fallback — `base` may not exist (fresh repo). Try without
            # explicit base so the new branch points at HEAD.
            res = run_command(
                ["git", "switch", "-c", branch],
                cwd=target,
                log_path=log,
                timeout_seconds=15,
            )
        if res.exit_code != 0:
            return GitOpResult(
                ok=False,
                exit_code=res.exit_code,
                detail={"reason": "branch_create_failed", "branch": branch},
            )
        events.write(
            "sut.git.branch_created",
            payload={
                "work_item_id": work_item_id,
                "branch": branch,
                "base": base,
                "sut_root": sut_root,
            },
        )
        return GitOpResult(ok=True, exit_code=0, detail={"branch": branch, "created": True})

    # Branch exists — switch to it (carries WIP across).
    res = run_command(
        ["git", "switch", branch],
        cwd=target,
        log_path=log,
        timeout_seconds=10,
    )
    if res.exit_code != 0:
        return GitOpResult(
            ok=False,
            exit_code=res.exit_code,
            detail={"reason": "branch_switch_failed", "branch": branch},
        )
    return GitOpResult(ok=True, exit_code=0, detail={"branch": branch, "switched": True})


def git_autocommit(
    paths: RuntimePaths,
    events: EventLog,
    *,
    sut_root: str,
    work_item_id: str,
    files: List[str],
    title: str,
    candidate_id: Optional[str] = None,
    sources: Optional[List[str]] = None,
) -> List[GitOpResult]:
    """Commit each generated file separately under `implementer-autopilot`.

    Issue #242. Skips silently when git is missing or the file is
    unchanged (no staged delta). Returns one GitOpResult per file.
    """
    if not git_available():
        _notify_skipped(events, "wi_autocommit", sut_root)
        return [_skipped_result()]
    target = _safe_sut_root(paths.repo_root, sut_root)
    if not has_git_repo(target):
        return [GitOpResult(ok=False, exit_code=-1, detail={"reason": "not_a_repo"})]

    results: List[GitOpResult] = []
    sources_line = ",".join(sources or []) if sources else ""
    candidate_line = candidate_id or ""

    for raw_path in files:
        if not isinstance(raw_path, str) or not raw_path:
            continue
        if raw_path.startswith("-") or ".." in Path(raw_path).parts:
            results.append(
                GitOpResult(ok=False, exit_code=-1, detail={"file": raw_path, "reason": "rejected_path"})
            )
            continue
        log = paths.subprocess_logs_dir / f"git-wi-commit-{now_iso().replace(':', '-')}.log"
        add_res = run_command(
            ["git", "add", "--", raw_path],
            cwd=target,
            log_path=log,
            timeout_seconds=15,
        )
        if add_res.exit_code != 0:
            results.append(
                GitOpResult(
                    ok=False,
                    exit_code=add_res.exit_code,
                    detail={"file": raw_path, "reason": "git_add_failed"},
                )
            )
            continue
        # Skip when nothing is staged (idempotent — the file is identical to HEAD).
        diff_res = run_command(
            ["git", "diff", "--cached", "--quiet", "--", raw_path],
            cwd=target,
            log_path=log,
            timeout_seconds=10,
        )
        if diff_res.exit_code == 0:
            results.append(
                GitOpResult(ok=True, exit_code=0, detail={"file": raw_path, "skipped": True, "reason": "no_change"})
            )
            continue
        area = Path(raw_path).parent.name or "tests"
        message_lines = [
            f"feat({area}): add {candidate_line or Path(raw_path).stem} — {title}",
            "",
            "actor: implementer-autopilot",
            f"work_item: {work_item_id}",
        ]
        if sources_line:
            message_lines.append(f"sources: {sources_line}")
        message = "\n".join(message_lines)
        commit_res = run_command(
            [
                "git",
                "-c",
                "user.email=agentic-os@local",
                "-c",
                "user.name=agentic-os",
                "commit",
                "-m",
                message,
                "--",
                raw_path,
            ],
            cwd=target,
            log_path=log,
            timeout_seconds=30,
        )
        if commit_res.exit_code != 0:
            results.append(
                GitOpResult(
                    ok=False,
                    exit_code=commit_res.exit_code,
                    detail={"file": raw_path, "reason": "git_commit_failed"},
                )
            )
            continue
        events.write(
            "sut.git.autocommit",
            payload={
                "work_item_id": work_item_id,
                "file": raw_path,
                "sut_root": sut_root,
                "candidate_id": candidate_id,
            },
        )
        results.append(
            GitOpResult(ok=True, exit_code=0, detail={"file": raw_path, "committed": True})
        )
    return results


def git_work_item_diff(
    paths: RuntimePaths,
    *,
    sut_root: str,
    work_item_id: str,
    title: Optional[str] = None,
    base: str = "main",
) -> Dict[str, object]:
    """Return `git diff base..agentic-os/wi-<id>-<slug>` as a unified diff.

    Best-effort read. Returns `{ok: bool, branch, diff?, error?}`.
    """
    if not git_available():
        return {"ok": False, "error": "git_not_installed"}
    target = _safe_sut_root(paths.repo_root, sut_root)
    if not has_git_repo(target):
        return {"ok": False, "error": "not_a_repo"}
    branch = work_item_branch_name(work_item_id, title)
    log = paths.subprocess_logs_dir / f"git-wi-diff-{now_iso().replace(':', '-')}.log"
    res = run_command(
        ["git", "diff", f"{base}..{branch}"],
        cwd=target,
        log_path=log,
        timeout_seconds=30,
    )
    if res.exit_code != 0:
        return {"ok": False, "branch": branch, "error": "diff_failed"}
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    body_lines = [line[len("[stdout] "):] for line in text.splitlines() if line.startswith("[stdout] ")]
    return {"ok": True, "branch": branch, "diff": "\n".join(body_lines)}


@dataclass(frozen=True)
class GitEnsureReport:
    ops: List[Dict[str, object]]
    summary: str
    ok: bool


def git_ensure(
    paths: RuntimePaths,
    events: EventLog,
    *,
    git_config: Dict[str, object],
    sut_root: str,
) -> GitEnsureReport:
    """Apply the declarative `git:` config block (#241).

    Idempotent: rerun on a green repo prints `no changes needed` (no ops
    fire). Short-circuits when `git.enabled=false` OR git binary missing.
    """
    enabled = bool(git_config.get("enabled", False))
    if not enabled:
        return GitEnsureReport(ops=[], summary="git integration disabled", ok=True)

    if not git_available():
        return GitEnsureReport(
            ops=[],
            summary="git binary missing — install or set git.enabled=false",
            ok=True,
        )

    ops: List[Dict[str, object]] = []
    auto_init = bool(git_config.get("auto_init", False))
    origin = git_config.get("origin")
    origin_branch = git_config.get("origin_branch", "main")
    auto_fetch = bool(git_config.get("auto_fetch", False))
    auto_publish = bool(git_config.get("auto_publish", False))
    if isinstance(origin_branch, str):
        _validate_branch_name(origin_branch)
    if origin is not None and not isinstance(origin, str):
        raise UsageError("git.origin must be a string or null")
    if isinstance(origin, str) and origin:
        validate_remote_url(origin)

    target = _safe_sut_root(paths.repo_root, sut_root)
    if not target.exists():
        raise UsageError(f"sut.root does not exist: {sut_root}")

    just_initialized = False
    initialized_already = has_git_repo(target)
    if not initialized_already:
        if auto_init:
            res = git_init(paths, events, sut_root=sut_root)
            ops.append({"op": "init", "ok": res.ok, "detail": res.detail})
            if not res.ok:
                return GitEnsureReport(ops=ops, summary="git init failed", ok=False)
            just_initialized = True
        else:
            return GitEnsureReport(
                ops=ops,
                summary="no .git found and git.auto_init=false; nothing to do",
                ok=True,
            )

    if isinstance(origin, str) and origin:
        res = git_set_remote(paths, events, sut_root=sut_root, remote_url=origin)
        ops.append({"op": "remote", "ok": res.ok, "detail": res.detail})
        if not res.ok:
            return GitEnsureReport(ops=ops, summary="git remote set failed", ok=False)

    if auto_publish and just_initialized:
        res = git_publish_main(paths, events, sut_root=sut_root)
        ops.append({"op": "publish", "ok": res.ok, "detail": res.detail})
        if not res.ok:
            return GitEnsureReport(ops=ops, summary="git publish failed", ok=False)
    elif auto_publish:
        ops.append({"op": "publish", "ok": True, "detail": {"skipped": True, "reason": "existing_repo"}})

    if auto_fetch and isinstance(origin, str) and origin:
        res = git_fetch(paths, events, sut_root=sut_root)
        ops.append({"op": "fetch", "ok": res.ok, "detail": res.detail})
        if not res.ok:
            return GitEnsureReport(ops=ops, summary="git fetch failed", ok=False)

    if not ops:
        return GitEnsureReport(ops=ops, summary="no changes needed", ok=True)

    ok_count = sum(1 for op in ops if op.get("ok"))
    summary = f"{ok_count}/{len(ops)} ok"
    events.write(
        "sut.git.ensure",
        payload={"sut_root": sut_root, "ops": list(ops), "summary": summary},
    )
    return GitEnsureReport(ops=ops, summary=summary, ok=ok_count == len(ops))


def git_status(
    paths: RuntimePaths,
    *,
    sut_root: str,
) -> Dict[str, object]:
    """Read-only status summary."""
    if not git_available():
        return {
            "initialized": False,
            "skipped": True,
            "reason": "git_not_installed",
        }
    target = _safe_sut_root(paths.repo_root, sut_root)
    if not has_git_repo(target):
        return {"initialized": False}
    log = paths.subprocess_logs_dir / "git-status-readonly.log"
    res = run_command(
        ["git", "rev-parse", "HEAD"],
        cwd=target,
        log_path=log,
        timeout_seconds=10,
    )
    head_sha: Optional[str] = None
    if res.exit_code == 0:
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if line.startswith("[stdout] "):
                    candidate = line[len("[stdout] "):].strip()
                    if candidate:
                        head_sha = candidate
                        break
        except OSError:
            pass
    remote_res = run_command(
        ["git", "remote", "get-url", "origin"],
        cwd=target,
        log_path=log,
        timeout_seconds=5,
    )
    remote_url: Optional[str] = None
    if remote_res.exit_code == 0:
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if line.startswith("[stdout] "):
                    candidate = line[len("[stdout] "):].strip()
                    if candidate:
                        remote_url = candidate
                        break
        except OSError:
            pass
    return {
        "initialized": True,
        "head_sha": head_sha,
        "remote_url": remote_url,
    }
