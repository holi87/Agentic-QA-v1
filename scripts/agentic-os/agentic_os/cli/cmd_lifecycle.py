"""Lifecycle commands: init, up, down, run, migrate-runtime (issue #292)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..errors import AgenticOSError, ConfigError, InfraError, ProductFailure, UsageError, UserAbort
from ..orchestrator import (
    CURRENT_PHASE_ID,
    fetch_active_leases,
    fetch_bug_summary,
    fetch_last_run,
    fetch_phase_rows,
    fetch_task_summary,
    list_open_blockers,
    open_runtime,
)
from ..paths import detect_repo_root, runtime_paths_from_config
from ..storage.db import SCHEMA_NAME, SCHEMA_VERSION, transaction
from ..time_utils import now_iso
from ..security import require_safe_argv, resolve_repo_path
from ..analysis import analyze_work_item
from ..patch_builder import implement_tests_for_work_item
from ..test_planning import (
    plan_work_item,
    read_plan_candidates,
    approve_all_runnable_candidates,
    update_plan_candidate_decision,
)
from ..work_items import (
    annotate_spec_status,
    create_work_item_from_file,
    get_work_item_detail,
    link_work_items,
    list_work_items,
    prune_orphan_work_items,
)
from ..inbox import ingest_inbox, list_inbox_files, synthesize_inbox_task
from ..workflows import run_dry_run, run_final_gate, run_recovery, run_review_gate, run_tests
from ._state import _active_config_override


def _prepare_init_config(repo_root: Path, *, force: bool) -> tuple[Path, bool, List[tuple[str, Dict[str, Any]]]]:
    """Create or migrate config before runtime bootstrap.

    `init` must not call `open_runtime()` until the final config path is
    known; otherwise a checkout with only legacy `.agentic-os/` runtime data
    can bootstrap SQLite in the legacy root while the freshly created config
    points subsequent commands at `agentic-os-runtime/`.
    """
    new_config_path = repo_root / "config" / "agentic-os.yml"
    new_example_path = repo_root / "config" / "agentic-os.yml.example"
    legacy_config_path = repo_root / ".qualitycat" / "agentic-os.yml"
    legacy_example_path = repo_root / ".qualitycat" / "agentic-os.yml.example"

    new_config_path.parent.mkdir(parents=True, exist_ok=True)
    config_created = False
    pending_events: List[tuple[str, Dict[str, Any]]] = []

    if new_config_path.exists() and not force:
        return new_config_path, config_created, pending_events

    if legacy_config_path.exists() and not new_config_path.exists():
        # Migrate legacy .qualitycat/ -> config/
        shutil.copyfile(legacy_config_path, new_config_path)
        if legacy_example_path.exists() and not new_example_path.exists():
            shutil.copyfile(legacy_example_path, new_example_path)
        # Drop migration marker so operator knows to clean .qualitycat/.
        marker = repo_root / ".qualitycat" / "MIGRATED.md"
        marker.write_text(
            "# Config migrated to `config/`\n\n"
            "Files in this directory are kept for fallback only. Once verified, delete.\n",
            encoding="utf-8",
        )
        pending_events.append(
            (
                "config.legacy_migrated",
                {
                    "from": str(legacy_config_path.relative_to(repo_root)),
                    "to": str(new_config_path.relative_to(repo_root)),
                },
            )
        )
        return new_config_path, True, pending_events

    example_to_copy = new_example_path if new_example_path.exists() else legacy_example_path
    if not example_to_copy.exists():
        raise InfraError(
            "config template missing: config/agentic-os.yml.example — "
            "restore it from git (`git checkout config/agentic-os.yml.example`) "
            "or re-clone the repo"
        )

    if new_config_path.exists() and force:
        backup = new_config_path.with_suffix(
            new_config_path.suffix + ".bak." + now_iso().replace(":", "-")
        )
        shutil.copyfile(new_config_path, backup)
        shutil.copyfile(example_to_copy, new_config_path)
        pending_events.append(
            (
                "config.created",
                {
                    "path": str(new_config_path.relative_to(repo_root)),
                    "backup": str(backup.relative_to(repo_root)),
                },
            )
        )
    else:
        shutil.copyfile(example_to_copy, new_config_path)
        pending_events.append(
            (
                "config.created",
                {"path": str(new_config_path.relative_to(repo_root))},
            )
        )
    config_created = True
    return new_config_path, config_created, pending_events


def _install_agentic_os_shim(
    repo_root: Path, *, shim_dir: Optional[Path], force: bool
) -> Dict[str, Any]:
    """Drop a thin wrapper into ``~/.local/bin`` (issue #139).

    The shim is a portable shell script that calls back into this
    checkout's Python module with the operator's ``$PWD`` preserved,
    so running ``agentic-os`` from anywhere routes to the version
    living next to ``config/agentic-os.yml``.
    """
    target_dir = shim_dir or Path.home() / ".local" / "bin"
    target_dir = target_dir.expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    shim_path = target_dir / "agentic-os"

    if shim_path.exists() and not force:
        raise UsageError(
            f"shim already exists at {shim_path}; pass --force to overwrite"
        )

    python = Path(sys.executable).resolve()
    src_dir = (repo_root / "scripts" / "agentic-os").resolve()
    if not (src_dir / "agentic_os" / "__main__.py").exists():
        raise InfraError(
            f"cannot find agentic_os package under {src_dir}; "
            f"run --install-shim from inside a checkout that has the CLI sources"
        )

    contents = (
        "#!/usr/bin/env bash\n"
        "# Auto-generated by `agentic-os init --install-shim` (issue #139).\n"
        "# Edit at your own risk — re-run with --force to regenerate.\n"
        "set -e\n"
        f"export PYTHONPATH=\"{src_dir}${{PYTHONPATH:+:$PYTHONPATH}}\"\n"
        f"exec \"{python}\" -m agentic_os \"$@\"\n"
    )
    shim_path.write_text(contents, encoding="utf-8")
    shim_path.chmod(0o755)

    on_path = any(
        Path(p).expanduser().resolve() == target_dir
        for p in (os.environ.get("PATH") or "").split(os.pathsep)
        if p
    )
    return {
        "shim_path": str(shim_path),
        "on_path": on_path,
        "python": str(python),
        "package_root": str(src_dir),
    }


def _install_sample_sut(repo_root: Path, *, force: bool = False) -> Dict[str, Any]:
    """Wave 15 (#315 / RC gap 4) — copy the sample SUT scaffold into the repo.

    Source lives under ``scripts/agentic-os/templates/sample-sut/``. The
    files land in ``<repo_root>/sample-sut/`` so a fresh checkout that
    ran ``agentic-os init --sample-sut`` can ``agentic-os up`` end-to-end
    without authoring any docker/openapi files. Returns metadata for the
    init payload; raises ``UsageError`` when the target directory exists
    and ``--force`` was not passed.
    """
    # Issue #292: this module moved from `agentic_os/cli.py` to
    # `agentic_os/cli/cmd_lifecycle.py`, so anchor on the package root
    # explicitly instead of walking `parent.parent` (which now points one
    # level too shallow).
    from .. import __file__ as _pkg_init
    package_root = Path(_pkg_init).resolve().parent
    template_root = package_root.parent / "templates" / "sample-sut"
    if not template_root.is_dir():
        raise InfraError(
            f"sample SUT template missing at {template_root}; "
            "restore via `git checkout scripts/agentic-os/templates/sample-sut`"
        )
    target_dir = repo_root / "sample-sut"
    if target_dir.exists() and not force:
        raise UsageError(
            f"{target_dir.relative_to(repo_root)}/ already exists; "
            "re-run with --force to overwrite"
        )
    target_dir.mkdir(parents=True, exist_ok=True)
    copied: List[str] = []
    for src in template_root.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(template_root)
        dst = target_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        copied.append(str(rel))
    # Rewrite config/agentic-os.yml so the sample-sut paths are the
    # active defaults. Best-effort: a malformed config never blocks
    # the file copy. The minimal config layout is YAML, so we patch
    # via a plain string merge to avoid pulling in a YAML round-trip
    # dependency that would also blow away operator-authored comments.
    cfg_path = repo_root / "config" / "agentic-os.yml"
    cfg_updated = False
    if cfg_path.is_file():
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            sut = data.setdefault("sut", {})
            sut["compose_file"] = "sample-sut/docker-compose.yml"
            sut.setdefault("compose_project_name", "agentic-os-sample-sut")
            sut.setdefault("autostart", False)
            web = sut.setdefault("web", {})
            web.setdefault("enabled", True)
            web.setdefault("url", "http://localhost:8080")
            api = sut.setdefault("api", {})
            api.setdefault("enabled", True)
            openapi = api.setdefault("openapi", {})
            existing_sources = openapi.get("sources") or []
            if "sample-sut/openapi.yaml" not in existing_sources:
                openapi["sources"] = [*existing_sources, "sample-sut/openapi.yaml"]
            cfg_path.write_text(
                yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
            )
            cfg_updated = True
        except Exception:  # pragma: no cover - defensive
            cfg_updated = False
    return {
        "target_dir": str(target_dir.relative_to(repo_root)),
        "files_copied": copied,
        "compose_file": "sample-sut/docker-compose.yml",
        "config_updated": cfg_updated,
        "config_path": str(cfg_path.relative_to(repo_root))
        if cfg_path.is_file()
        else None,
    }


def cmd_init(repo_root: Path, args: List[str], *, json_output: bool) -> int:
    sub = argparse.ArgumentParser(prog="agentic-os init", add_help=True)
    sub.add_argument("--force", action="store_true")
    sub.add_argument("--install-shim", action="store_true")
    sub.add_argument(
        "--shim-dir",
        default=None,
        help="Override the directory where --install-shim drops the wrapper (default: ~/.local/bin)",
    )
    sub.add_argument(
        "--sample-sut",
        action="store_true",
        help=(
            "Wave 15 (#315 / RC gap 4) — copy a working sample SUT "
            "(docker-compose + httpbin/nginx) into ./sample-sut/ and "
            "rewrite config/agentic-os.yml to point at it so "
            "`agentic-os up` works end-to-end on a fresh checkout."
        ),
    )
    opts = sub.parse_args(args)

    sample_sut_info: Optional[Dict[str, Any]] = None
    if opts.sample_sut:
        sample_sut_info = _install_sample_sut(repo_root, force=opts.force)

    if opts.install_shim:
        shim_info = _install_agentic_os_shim(
            repo_root,
            shim_dir=Path(opts.shim_dir) if opts.shim_dir else None,
            force=opts.force,
        )
        if json_output:
            sys.stdout.write(json.dumps({"shim": shim_info}, indent=2, sort_keys=True) + "\n")
        else:
            sys.stdout.write(
                f"shim installed at: {shim_info['shim_path']}\n"
                f"  package_root:    {shim_info['package_root']}\n"
                f"  python:          {shim_info['python']}\n"
            )
            if not shim_info["on_path"]:
                sys.stdout.write(
                    "  warning: shim directory is not on $PATH; "
                    "add it to your shell rc to invoke `agentic-os` directly\n"
                )
        # --install-shim is a sub-action of init; the regular config /
        # runtime bootstrap still runs below so the operator gets a
        # working CLI in one command.

    config_path, config_created, pending_events = _prepare_init_config(repo_root, force=opts.force)
    conn, paths, events, orch = open_runtime(repo_root)
    try:
        events.write(
            "runtime.initialized",
            payload={"repo_root": str(repo_root), "runtime_root": str(paths.runtime_root)},
        )
        events.write(
            "db.migration_applied",
            payload={"version": SCHEMA_VERSION, "name": SCHEMA_NAME},
        )
        for event_name, payload in pending_events:
            events.write(event_name, payload=payload)

        orch.seed_phases()

        payload = {
            "repo_root": str(repo_root),
            "runtime_root": str(paths.runtime_root.relative_to(repo_root)),
            "config_path": str(config_path.relative_to(repo_root)),
            "config_created": config_created,
        }
        if sample_sut_info is not None:
            payload["sample_sut"] = sample_sut_info
        if json_output:
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        else:
            sys.stdout.write(
                "init ok\n"
                f"  runtime_root: {payload['runtime_root']}\n"
                f"  config_path:  {payload['config_path']} (created={config_created})\n"
            )
            if sample_sut_info is not None:
                sys.stdout.write(
                    f"  sample SUT:   {sample_sut_info['target_dir']} "
                    f"({len(sample_sut_info['files_copied'])} files; "
                    f"compose at {sample_sut_info['compose_file']})\n"
                    "  bring it up:  agentic-os up\n"
                )
        return 0
    finally:
        conn.close()


_DASHBOARD_PIDFILE_NAME = "dashboard.pid"

_DASHBOARD_LOGFILE_NAME = "dashboard.log"


def _dashboard_pid_path(repo_root: Path, config_override: Optional[Path]) -> Path:
    paths = runtime_paths_from_config(repo_root, override=config_override)
    return paths.pids_dir / _DASHBOARD_PIDFILE_NAME


def _dashboard_log_path(repo_root: Path, config_override: Optional[Path]) -> Path:
    paths = runtime_paths_from_config(repo_root, override=config_override)
    return paths.logs_dir / _DASHBOARD_LOGFILE_NAME


def _process_alive(pid: int) -> bool:
    """Return True when the OS still owns the pid.

    ``os.kill(pid, 0)`` is the POSIX sniff-test — raises ``ProcessLookupError``
    when the pid is gone, ``PermissionError`` when it exists but is owned by
    another user (still alive from our perspective).
    """
    import os as _os

    try:
        _os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _spawn_dashboard_daemon(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path],
) -> int:
    """Re-exec this CLI with ``--foreground`` in a detached subprocess.

    The child runs the same ``up`` command minus the ``--daemon`` flag.
    Stdout/stderr are redirected to ``runtime/logs/dashboard.log`` so
    operators have a single file to ``tail -f`` from `cmd_logs`.
    """
    import os as _os
    import subprocess
    import time as _time

    pid_path = _dashboard_pid_path(repo_root, config_override)
    log_path = _dashboard_log_path(repo_root, config_override)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if pid_path.exists():
        try:
            existing_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            existing_pid = 0
        if existing_pid and _process_alive(existing_pid):
            raise UsageError(
                f"dashboard daemon already running (pid={existing_pid}); "
                f"stop it with `agentic-os down`"
            )
        # Stale pidfile — clean up before claiming the slot.
        pid_path.unlink(missing_ok=True)

    child_args = [a for a in args if a != "--daemon"]
    # Foreground in the child — the daemon wrapper provides the
    # detachment; ``--foreground`` keeps `serve_blocking` in the call
    # chain so the dashboard actually serves requests in the child.
    if "--foreground" not in child_args:
        child_args.append("--foreground")
    cmd = [sys.executable, "-m", "agentic_os", "--root", str(repo_root)]
    if config_override is not None:
        cmd.extend(["--config", str(config_override)])
    cmd.extend(["up", *child_args])

    log_file = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            cwd=str(repo_root),
        )
    finally:
        log_file.close()

    pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")

    # Give the child a brief moment to fail fast (config error, port in
    # use, etc.) so the operator sees a clear failure instead of a
    # silently-dead pidfile.
    _time.sleep(0.3)
    if not _process_alive(proc.pid):
        pid_path.unlink(missing_ok=True)
        # Surface whatever the child wrote so the operator knows why.
        tail = ""
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-2048:]
        except OSError:
            pass
        raise InfraError(
            f"dashboard daemon exited immediately after spawn; "
            f"see {log_path}\n--- tail ---\n{tail}"
        )

    payload = {
        "ok": True,
        "pid": proc.pid,
        "pidfile": str(pid_path.relative_to(repo_root)) if pid_path.is_relative_to(repo_root) else str(pid_path),
        "log": str(log_path.relative_to(repo_root)) if log_path.is_relative_to(repo_root) else str(log_path),
    }
    if json_output:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            f"dashboard daemonized as pid {proc.pid}\n"
            f"  pidfile: {payload['pidfile']}\n"
            f"  log:     {payload['log']}\n"
            f"  stop:    agentic-os down\n"
            f"  tail:    agentic-os logs --follow\n"
        )
    return 0


def cmd_up(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    sub = argparse.ArgumentParser(prog="agentic-os up", add_help=True)
    sub.add_argument("--foreground", action="store_true")
    sub.add_argument("--dashboard-only", action="store_true")
    sub.add_argument("--stop-existing", action="store_true")
    sub.add_argument("--host", default=None)
    sub.add_argument("--port", type=int, default=None)
    sub.add_argument(
        "--daemon",
        action="store_true",
        help=(
            "Run the dashboard as a detached background process (issue #139). "
            "Pidfile lands under runtime/pids/dashboard.pid and stdout/stderr "
            "are redirected to runtime/logs/dashboard.log."
        ),
    )
    sub.add_argument(
        "--full",
        action="store_true",
        help="Enable all dashboard write endpoints for this session (in-memory override).",
    )
    sub.add_argument(
        "--no-autostart",
        action="store_true",
        help="Skip SUT autostart even when sut.autostart=true in config.",
    )
    sub.add_argument(
        "--auto-repair",
        action="store_true",
        help=(
            "Issue #274 — apply ONLY the safe repairs (stale lease clear, "
            "orphan spec delete) during the startup sweep. Hard repairs still "
            "require `doctor --repair --yes`."
        ),
    )
    sub.add_argument(
        "--autonomy-minutes",
        type=int,
        default=None,
        help=(
            "Wave 15 (#315 / RC gap 7) — start an autonomy session alongside "
            "the dashboard for N minutes. Defaults to 480 (8h) when `up` is "
            "invoked without --dashboard-only; refused under --dashboard-only "
            "so the explicit dashboard-only mode stays a true read-only "
            "operator console."
        ),
    )
    opts = sub.parse_args(args)

    # Issue #315 (Wave 15) — `up` without `--dashboard-only` is the
    # orchestrator-daemon path: it starts the dashboard PLUS an autonomy
    # session in the same process so a single `agentic-os up` brings the
    # system live and a single `agentic-os down` brings it back to rest.
    # The autonomy MANAGER is a module-level in-memory singleton, so it
    # has to live inside the dashboard process; a separate worker process
    # would not share the singleton.
    start_autonomy = not opts.dashboard_only
    autonomy_minutes = opts.autonomy_minutes
    if start_autonomy and autonomy_minutes is None:
        autonomy_minutes = 480  # 8h default; operator can shorten
    if opts.dashboard_only and opts.autonomy_minutes is not None:
        raise UsageError(
            "--autonomy-minutes is incompatible with --dashboard-only "
            "(dashboard-only mode never runs an autonomy session)"
        )

    if opts.daemon and opts.foreground:
        raise UsageError("--daemon is incompatible with --foreground")

    if opts.daemon:
        return _spawn_dashboard_daemon(
            repo_root,
            args,
            json_output=json_output,
            config_override=config_override,
        )

    from ..config import ConfigError, load_or_default
    from ..server import DEFAULT_HOST, DEFAULT_PORT, serve_blocking, set_full_mode_override

    host: Optional[str] = opts.host
    port: Optional[int] = opts.port
    sut_cfg: Dict[str, Any] = {}
    try:
        cfg = load_or_default(repo_root, override=config_override)
        host = host or cfg.dashboard_host
        port = port or cfg.dashboard_port
        sut_cfg = cfg.raw.get("sut") or {}
    except ConfigError:
        # config not present yet (e.g., before init) — fall back to defaults
        pass
    host = host or DEFAULT_HOST
    port = port or DEFAULT_PORT

    # --full toggles in-memory write endpoint override for this run.
    if opts.full:
        set_full_mode_override(True)

    conn, paths, events, _ = open_runtime(repo_root)
    try:
        events.write(
            "dashboard.starting",
            payload={
                "host": host,
                "port": int(port),
                "foreground": bool(opts.foreground),
                "full_mode": bool(opts.full),
            },
        )
        # Issue #274 — startup repair sweep. Always run a dry-run scan and
        # report the counts. With --auto-repair, ALSO apply the safe-only
        # repairs (stale lease clear / orphan spec delete); hard repairs still
        # require `doctor --repair --yes`. Best-effort: a scan failure must
        # never block the dashboard from coming up.
        try:
            from .. import repair as _repair

            if opts.auto_repair:
                sweep = _repair.repair(conn, paths, events, apply=True, safe_only=True)
            else:
                sweep = _repair.repair(conn, paths, events, apply=False)
            applied = len(sweep.get("applied") or [])
            if not json_output:
                if opts.auto_repair:
                    sys.stdout.write(
                        f"startup repair sweep: {sweep['total']} finding(s), "
                        f"{applied} safe repair(s) applied\n"
                    )
                else:
                    sys.stdout.write(
                        f"startup repair sweep (dry-run): {sweep['total']} finding(s) "
                        f"(safe={sweep['safe_count']} hard={sweep['hard_count']}); "
                        f"run `agentic-os doctor --repair --yes` to apply\n"
                    )
        except Exception as exc:  # pragma: no cover - defensive
            events.write(
                "doctor.repair.sweep_skipped",
                severity="warning",
                payload={"reason": str(exc)},
            )
        # Autostart SUT when full mode OR config flag, unless suppressed.
        autostart_requested = bool(opts.full or sut_cfg.get("autostart"))
        if autostart_requested and not opts.no_autostart and sut_cfg.get("compose_file"):
            from ..sut_lifecycle import run_sut_start

            res = run_sut_start(
                paths,
                events,
                compose_file=sut_cfg.get("compose_file"),
                compose_project_name=sut_cfg.get("compose_project_name"),
            )
            if not res.ok:
                events.write(
                    "dashboard.sut_autostart_skipped",
                    severity="warning",
                    payload={"reason": res.failure_kind or "unknown"},
                )
    finally:
        conn.close()

    # Issue #315 (Wave 15) — orchestrator-daemon path: kick off the
    # autonomy session BEFORE serve_blocking takes over the main thread.
    # `start_session` returns immediately (the loop runs on a background
    # thread); SIGTERM from `agentic-os down` tears the process down and
    # the daemon thread with it. Best-effort: a failed autonomy start
    # must not block the dashboard.
    autonomy_state = None
    if start_autonomy:
        try:
            from .. import autonomy as _autonomy

            autonomy_state = _autonomy.start_session(
                runtime_paths_from_config(repo_root, override=config_override),
                max_minutes=int(autonomy_minutes),
            )
        except Exception as exc:  # pragma: no cover - defensive
            sys.stderr.write(
                f"warning: autonomy session failed to start: {exc}\n"
                "dashboard will still come up; start the loop manually via "
                "`agentic-os autonomy start` or POST /api/autonomy/start.\n"
            )

    if json_output:
        sys.stdout.write(
            json.dumps(
                {
                    "ok": True,
                    "host": host,
                    "port": int(port),
                    "full_mode": bool(opts.full),
                    "autonomy": {
                        "started": autonomy_state is not None,
                        "session_id": getattr(autonomy_state, "session_id", None),
                        "max_minutes": int(autonomy_minutes) if start_autonomy else None,
                    },
                }
            )
            + "\n"
        )
    else:
        suffix = " [FULL MODE]" if opts.full else ""
        sys.stdout.write(f"dashboard on http://{host}:{port}{suffix} (Ctrl+C to stop)\n")
        if autonomy_state is not None:
            sys.stdout.write(
                f"autonomy session {autonomy_state.session_id} running for "
                f"{autonomy_minutes} minutes (`agentic-os down` stops both)\n"
            )
    sys.stdout.flush()
    return serve_blocking(runtime_paths_from_config(repo_root, override=config_override), host=host, port=int(port))


def cmd_down(repo_root: Path, args: List[str], *, json_output: bool) -> int:
    """Stop the dashboard daemon launched via ``up --daemon`` (issue #139)."""
    import os as _os
    import signal as _signal
    import time as _time

    sub = argparse.ArgumentParser(prog="agentic-os down", add_help=True)
    sub.add_argument("--stop-sut", action="store_true")
    sub.add_argument("--volumes", action="store_true")
    sub.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Seconds to wait for SIGTERM before escalating to SIGKILL (default 5)",
    )
    opts = sub.parse_args(args)

    pid_path = _dashboard_pid_path(repo_root, None)
    if not pid_path.exists():
        payload = {"ok": True, "reason": "no_pidfile"}
        if json_output:
            sys.stdout.write(json.dumps(payload) + "\n")
        else:
            sys.stdout.write("down: no daemon pidfile (nothing to stop)\n")
        return 0

    try:
        pid_text = pid_path.read_text(encoding="utf-8").strip()
        pid = int(pid_text)
    except (OSError, ValueError) as exc:
        raise InfraError(f"corrupt pidfile at {pid_path}: {exc}")

    if not _process_alive(pid):
        pid_path.unlink(missing_ok=True)
        payload = {"ok": True, "reason": "stale_pidfile", "pid": pid}
        if json_output:
            sys.stdout.write(json.dumps(payload) + "\n")
        else:
            sys.stdout.write(f"down: pid {pid} not running (stale pidfile cleared)\n")
        return 0

    escalated = False
    try:
        _os.kill(pid, _signal.SIGTERM)
    except ProcessLookupError:
        pid_path.unlink(missing_ok=True)
        if json_output:
            sys.stdout.write(json.dumps({"ok": True, "reason": "race_lost", "pid": pid}) + "\n")
        else:
            sys.stdout.write(f"down: pid {pid} exited before SIGTERM landed\n")
        return 0

    deadline = _time.monotonic() + max(0.1, opts.timeout)
    while _time.monotonic() < deadline:
        if not _process_alive(pid):
            break
        _time.sleep(0.1)

    if _process_alive(pid):
        escalated = True
        try:
            _os.kill(pid, _signal.SIGKILL)
        except ProcessLookupError:
            pass
        # Brief settle so the kernel cleans up before the operator
        # potentially restarts immediately.
        _time.sleep(0.1)

    pid_path.unlink(missing_ok=True)
    payload = {
        "ok": True,
        "pid": pid,
        "escalated_to_sigkill": escalated,
    }
    if json_output:
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    else:
        if escalated:
            sys.stdout.write(f"down: pid {pid} killed (SIGTERM timed out, SIGKILL sent)\n")
        else:
            sys.stdout.write(f"down: pid {pid} stopped (SIGTERM)\n")
    return 0


def cmd_run(
    repo_root: Path,
    args: List[str],
    *,
    json_output: bool,
    config_override: Optional[Path] = None,
) -> int:
    sub = argparse.ArgumentParser(prog="agentic-os run", add_help=True)
    sub.add_argument("workflow")
    sub.add_argument("--phase", default=CURRENT_PHASE_ID)
    sub.add_argument("--tag", default=None)
    sub.add_argument("--dry", action="store_true")
    sub.add_argument("--retry-of", default=None)
    sub.add_argument("--fake-sut", action="store_true")
    sub.add_argument("--scope", default="general", choices=("api", "ui", "assertion", "final", "general"))
    sub.add_argument("--diff", type=Path, default=None)
    sub.add_argument("--reviewer-output", type=Path, default=None)
    sub.add_argument("--apply-patch", type=Path, default=None)
    sub.add_argument("--work-item", default=None)
    opts = sub.parse_args(args)

    conn, paths, events, orch = open_runtime(repo_root)
    try:
        if opts.workflow == "dry-run":
            result = run_dry_run(orch, paths, events, fake_sut=opts.fake_sut)
        elif opts.workflow == "recovery":
            result = run_recovery(orch, paths, events)
        elif opts.workflow == "run-tests":
            result = run_tests(
                orch, paths, events, tag=opts.tag, work_item_id=opts.work_item
            )
        elif opts.workflow == "review-gate":
            result = run_review_gate(
                orch,
                paths,
                events,
                diff_path=opts.diff,
                scope=opts.scope,
                reviewer_output_path=opts.reviewer_output,
                apply_patch_path=opts.apply_patch,
                work_item_id=opts.work_item,
            )
        elif opts.workflow == "final-gate":
            result = run_final_gate(orch, paths, events, work_item_id=opts.work_item)
        elif opts.workflow in {"sut-start", "sut-healthcheck", "sut-stop"}:
            return _run_sut_workflow(repo_root, paths, events, opts.workflow, json_output)
        else:
            raise UsageError(f"unknown workflow '{opts.workflow}'")
    finally:
        conn.close()

    if json_output:
        sys.stdout.write(json.dumps(result.to_json(), indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            f"workflow={opts.workflow} ok={result.ok} exit={result.exit_code}\n"
            f"  task_id:      {result.task_id}\n"
            f"  run_id:       {result.run_id}\n"
            f"  manifest:     {result.manifest_path}\n"
        )
    return result.exit_code


def _run_sut_workflow(
    repo_root: Path,
    paths: "RuntimePaths",
    events: "EventLog",
    workflow: str,
    json_output: bool,
) -> int:
    from ..config import load_or_default
    from ..sut_lifecycle import run_sut_healthcheck, run_sut_start, run_sut_stop

    try:
        cfg = load_or_default(repo_root, override=_active_config_override()).raw
    except Exception as exc:
        raise UsageError(f"cannot load config: {exc}")
    sut = cfg.get("sut") or {}
    if workflow == "sut-start":
        res = run_sut_start(
            paths,
            events,
            compose_file=sut.get("compose_file"),
            compose_project_name=sut.get("compose_project_name"),
        )
    elif workflow == "sut-stop":
        res = run_sut_stop(
            paths,
            events,
            compose_file=sut.get("compose_file"),
            compose_project_name=sut.get("compose_project_name"),
        )
    else:  # sut-healthcheck
        hc = sut.get("healthcheck") or {}
        res = run_sut_healthcheck(
            paths,
            events,
            command=hc.get("command") or [],
            timeout_seconds=int(hc.get("timeout_seconds") or 30),
            retries=int(hc.get("retries") or 0),
        )
    payload = {
        "workflow": workflow,
        "ok": res.ok,
        "exit_code": res.exit_code,
        "failure_kind": res.failure_kind,
        "log_path": str(res.log_path.relative_to(paths.repo_root)) if res.log_path else None,
        "detail": res.detail,
    }
    if json_output:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            f"workflow={workflow} ok={res.ok} exit={res.exit_code}\n"
        )
    return res.exit_code


def cmd_migrate_runtime(repo_root: Path, args: List[str], *, json_output: bool) -> int:
    """Move ``.agentic-os/`` runtime to ``agentic-os-runtime/`` (issue #142).

    The legacy runtime root predates the visible default. Operators who
    bootstrapped before the change end up with state in a hidden
    directory; new operators get the visible one. Running both side by
    side leads to silent debugging on stale data.

    This command consolidates onto the visible default:

    1. Refuses without ``--force`` if both runtimes already contain a
       state DB — there is no safe automatic merge, so the operator
       must pick a winner.
    2. Otherwise copies the legacy tree into ``agentic-os-runtime/``
       (or refuses if the destination exists), then archives the
       legacy directory as ``.agentic-os.legacy-<UTC>/`` so the
       operator can confirm before deleting.

    ``--dry-run`` reports the plan without touching the filesystem.
    """
    from ..paths import DEFAULT_RUNTIME_ROOT, LEGACY_RUNTIME_ROOT

    sub = argparse.ArgumentParser(prog="agentic-os migrate-runtime", add_help=True)
    sub.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the migration plan without modifying any files",
    )
    sub.add_argument(
        "--force",
        action="store_true",
        help="Allow migration even when the visible runtime is already populated",
    )
    opts = sub.parse_args(args)

    visible = repo_root / DEFAULT_RUNTIME_ROOT
    legacy = repo_root / LEGACY_RUNTIME_ROOT

    legacy_state = legacy / "state.db"
    visible_state = visible / "state.db"
    timestamp = now_iso().replace(":", "-").replace("+", "p")
    archived = repo_root / f"{LEGACY_RUNTIME_ROOT}.legacy-{timestamp}"

    plan: Dict[str, Any] = {
        "legacy_exists": legacy.exists(),
        "visible_exists": visible.exists(),
        "legacy_has_db": legacy_state.exists(),
        "visible_has_db": visible_state.exists(),
        "dry_run": opts.dry_run,
        "force": opts.force,
        "actions": [],
        "archived_to": str(archived.relative_to(repo_root)),
        "status": "no-op",
    }

    if not legacy.exists():
        plan["status"] = "nothing-to-migrate"
        plan["reason"] = (
            f"{LEGACY_RUNTIME_ROOT}/ does not exist; nothing to do"
        )
        if json_output:
            sys.stdout.write(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        else:
            sys.stdout.write(plan["reason"] + "\n")
        return 0

    if legacy_state.exists() and visible_state.exists() and not opts.force:
        plan["status"] = "blocked"
        plan["reason"] = (
            f"both {LEGACY_RUNTIME_ROOT}/state.db and {DEFAULT_RUNTIME_ROOT}/state.db "
            f"exist; refusing to merge SQLite state automatically. "
            f"Pick one source of truth, archive the other manually, then re-run."
        )
        if json_output:
            sys.stdout.write(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        else:
            sys.stdout.write("error: " + plan["reason"] + "\n")
        return 2

    # Build the action list before touching disk so dry-run is honest
    # about exactly what would happen — including the destructive
    # `--force` archive path (codex review on #142).
    visible_archived: Optional[Path] = None
    clobber_archive: Optional[Path] = None
    if visible.exists() and not opts.force:
        # Visible exists but has no DB — treat it as a stub from `init`.
        # Archive it next to the legacy tree to keep the destination
        # clean for the copy.
        visible_archived = repo_root / f"{DEFAULT_RUNTIME_ROOT}.pre-migrate-{timestamp}"
        plan["actions"].append(
            {"op": "archive_visible", "from": DEFAULT_RUNTIME_ROOT, "to": str(visible_archived.relative_to(repo_root))}
        )
    elif visible.exists() and opts.force:
        # `--force` with a populated visible runtime: the operator told
        # us to clobber it. Archive it for safety anyway so nothing is
        # lost without a marker on disk.
        clobber_archive = repo_root / f"{DEFAULT_RUNTIME_ROOT}.clobbered-{timestamp}"
        plan["actions"].append(
            {"op": "force_archive_visible", "from": DEFAULT_RUNTIME_ROOT, "to": str(clobber_archive.relative_to(repo_root))}
        )

    plan["actions"].append(
        {"op": "copy_tree", "from": LEGACY_RUNTIME_ROOT, "to": DEFAULT_RUNTIME_ROOT}
    )
    plan["actions"].append(
        {"op": "archive_legacy", "from": LEGACY_RUNTIME_ROOT, "to": plan["archived_to"]}
    )

    if opts.dry_run:
        plan["status"] = "dry-run"
        if json_output:
            sys.stdout.write(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        else:
            sys.stdout.write(
                f"would migrate {LEGACY_RUNTIME_ROOT}/ → {DEFAULT_RUNTIME_ROOT}/ "
                f"and archive legacy to {plan['archived_to']}\n"
            )
        return 0

    # --- execute ----------------------------------------------------
    if visible_archived is not None:
        shutil.move(str(visible), str(visible_archived))
    elif clobber_archive is not None:
        shutil.move(str(visible), str(clobber_archive))

    shutil.copytree(legacy, visible)
    shutil.move(str(legacy), str(archived))

    plan["status"] = "migrated"
    if json_output:
        sys.stdout.write(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            f"migrated {LEGACY_RUNTIME_ROOT}/ → {DEFAULT_RUNTIME_ROOT}/\n"
            f"  legacy archived at: {plan['archived_to']}\n"
            f"  verify, then delete the archive to reclaim disk space\n"
        )
    return 0
