"""Runtime path layout helpers — see runtime-contract.md section 2."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_RUNTIME_ROOT = "agentic-os-runtime"
LEGACY_RUNTIME_ROOT = ".agentic-os"


@dataclass(frozen=True)
class RuntimePaths:
    repo_root: Path
    runtime_root: Path

    @property
    def db(self) -> Path:
        return self.runtime_root / "state.db"

    @property
    def events_dir(self) -> Path:
        return self.runtime_root / "events"

    @property
    def logs_dir(self) -> Path:
        return self.runtime_root / "logs"

    @property
    def subprocess_logs_dir(self) -> Path:
        return self.logs_dir / "subprocess"

    @property
    def patches_dir(self) -> Path:
        return self.runtime_root / "patches"

    @property
    def worktree_dir(self) -> Path:
        return self.runtime_root / "worktree"

    @property
    def evidence_dir(self) -> Path:
        return self.runtime_root / "evidence"

    @property
    def backups_dir(self) -> Path:
        return self.runtime_root / "backups"

    @property
    def leases_dir(self) -> Path:
        return self.runtime_root / "leases"

    @property
    def pids_dir(self) -> Path:
        return self.runtime_root / "pids"

    @property
    def tmp_dir(self) -> Path:
        return self.runtime_root / "tmp"

    @property
    def task_specs_dir(self) -> Path:
        return self.runtime_root / "task-specs"

    def ensure(self) -> None:
        for p in (
            self.runtime_root,
            self.events_dir,
            self.logs_dir,
            self.subprocess_logs_dir,
            self.patches_dir,
            self.worktree_dir,
            self.evidence_dir,
            self.backups_dir,
            self.leases_dir,
            self.pids_dir,
            self.tmp_dir,
            self.task_specs_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


def detect_repo_root(start: Path) -> Path:
    cur = start.resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / ".git").exists():
            return candidate
    return cur


def runtime_paths(repo_root: Path, runtime_root_rel: str = DEFAULT_RUNTIME_ROOT) -> RuntimePaths:
    return RuntimePaths(repo_root=repo_root, runtime_root=repo_root / runtime_root_rel)


def runtime_paths_from_config(repo_root: Path, *, override: Optional[Path] = None) -> RuntimePaths:
    """Issue #97 — honor `runtime.root` from the loaded config.

    Falls back to the visible default runtime dir when config is missing
    or invalid so callers never crash on a fresh checkout. If a legacy
    `.agentic-os/` runtime exists and the visible runtime does not, keep
    reading the legacy runtime for compatibility. Errors are swallowed
    by design: this function must be a drop-in replacement for the legacy
    `runtime_paths(repo_root)` call site.
    """
    rel = DEFAULT_RUNTIME_ROOT
    try:
        from .config import load_or_default

        cfg = load_or_default(repo_root, override=override)
        runtime_cfg = cfg.raw.get("runtime") or {}
        configured = runtime_cfg.get("root")
        if isinstance(configured, str) and configured.strip():
            rel = configured.strip()
    except Exception:
        if (repo_root / LEGACY_RUNTIME_ROOT).exists() and not (repo_root / DEFAULT_RUNTIME_ROOT).exists():
            rel = LEGACY_RUNTIME_ROOT
    return RuntimePaths(repo_root=repo_root, runtime_root=repo_root / rel)
