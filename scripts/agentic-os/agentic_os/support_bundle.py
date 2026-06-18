"""Support-bundle builder — collects redacted diagnostics into one tarball
that an operator can attach to an issue or share with the maintainers.

The bundle deliberately captures the operator's CURRENT state (config,
doctor, recent events, last run, bug files). It is NOT a snapshot of the
full repository.

Redaction policy
----------------

Config secrets are redacted by **leaf-key match against a denylist regex**:
any leaf whose key matches the pattern is replaced with the literal string
``<redacted>``. The denylist favours over-redaction — leaking an opaque
config key is recoverable, leaking a real API token is not. The disclaimer
the dashboard renders next to the download button is the contract:
*"basics are redacted, but review the bundle before sending."*

Outputs (relative to the tarball root):

- ``MANIFEST.json``        — what was included + per-file sizes + decisions
- ``config/agentic-os.yml`` — REDACTED canonical (or legacy) config
- ``doctor.json``          — `build_doctor_payload(include_sut+models+docker)`
- ``events/<file>.jsonl``  — tail of each `events/*.jsonl` (capped per file)
- ``runs/<latest>/...``    — last run manifest + small artifacts
- ``bugs/*.json``          — bug records that exist on disk

Caps:

- per file in the bundle: ``_PER_FILE_BYTE_CAP`` (256 KiB). Larger files are
  truncated and the manifest marks them ``truncated: true`` with the
  original size, so the operator knows to attach the full artifact
  separately if it matters.
- total tarball cap is not enforced server-side; instead the manifest gives
  the operator the data they need to decide whether to share it.
"""
from __future__ import annotations

import io
import json
import re
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .paths import RuntimePaths


SUPPORT_BUNDLE_DIRNAME = "support-bundles"

# Subsystems the bundle may gather. Operators select via --include / --exclude
# on the CLI (issue #180). `config` and `doctor` are environment context;
# `events`, `runs`, `bugs` are operator state captured from the runtime.
SUPPORT_BUNDLE_SUBSYSTEMS: frozenset[str] = frozenset(
    {"config", "doctor", "events", "runs", "bugs"}
)

# Leaf-key denylist. Match is case-insensitive on the *final* key in any
# dotted path through the YAML structure. Pattern is intentionally broad —
# the cost of an unnecessary `<redacted>` is a follow-up question from the
# maintainer, the cost of a leak is a rotated credential.
_REDACT_LEAF_RE = re.compile(
    r"(api[_-]?key|apikey|secret|password|passwd|token|bearer|"
    r"credential|access[_-]?key|private[_-]?key|client[_-]?secret)",
    re.IGNORECASE,
)
_REDACT_PLACEHOLDER = "<redacted>"

_PER_FILE_BYTE_CAP = 256 * 1024
_EVENT_TAIL_LINES = 500


@dataclass
class _BundledFile:
    arcname: str
    source: Optional[str]  # path inside repo, or None for generated files
    bytes_in_bundle: int
    original_bytes: Optional[int]
    truncated: bool
    note: Optional[str] = None


def redact_config(data: Any) -> Any:
    """Return a deep copy of `data` with secret-shaped leaves replaced.

    Works on the YAML-decoded shape (dict / list / scalars). Tuples are not
    expected from the YAML loader. Anything not understood is returned as
    `<redacted-unknown-type>` to avoid leaking by accident.
    """
    if isinstance(data, dict):
        return {
            key: (
                _REDACT_PLACEHOLDER
                if isinstance(key, str) and _REDACT_LEAF_RE.search(key)
                else redact_config(value)
            )
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [redact_config(item) for item in data]
    if isinstance(data, (str, int, float, bool)) or data is None:
        return data
    return "<redacted-unknown-type>"


_TAG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _bundle_filename(now: Optional[datetime] = None, *, tag: Optional[str] = None) -> str:
    when = now or datetime.now(timezone.utc)
    stem = f"support-{when.strftime('%Y%m%dT%H%M%SZ')}"
    if tag:
        stem = f"{stem}-{tag}"
    return f"{stem}.tar.gz"


def _read_capped(path: Path, *, cap: int = _PER_FILE_BYTE_CAP) -> Tuple[bytes, bool, int]:
    """Read up to `cap` bytes from `path`; return (data, truncated, original_size)."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        data = fh.read(cap)
    truncated = size > cap
    return data, truncated, size


def _tail_jsonl_lines(path: Path, *, lines: int = _EVENT_TAIL_LINES, cap: int = _PER_FILE_BYTE_CAP) -> Tuple[bytes, bool, int]:
    """Return the last `lines` newline-delimited records of a JSONL file,
    capped at `cap` bytes."""
    original = path.stat().st_size
    text = path.read_text(encoding="utf-8", errors="replace")
    rows = text.splitlines()
    tail = rows[-lines:]
    encoded = ("\n".join(tail) + ("\n" if tail else "")).encode("utf-8")
    if len(encoded) > cap:
        encoded = encoded[-cap:]
        truncated = True
    else:
        truncated = len(rows) > lines or original > len(encoded)
    return encoded, truncated, original


def _config_bundle_bytes(
    repo_root: Path, *, redact: bool = True
) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Locate the active config and return its YAML bytes for the bundle.

    Returns (bytes, source_relative_path, note). `note` is set when no
    config could be found (still bundle a placeholder so the operator
    sees the gap). When `redact=False` the raw file contents are embedded
    verbatim — the `--no-redact` CLI flag is the only path that toggles
    this and exists for offline triage where the operator owns the
    bundle. The manifest annotates which mode was used so reviewers
    cannot mistake an unredacted bundle for a redacted one."""
    candidates = [
        repo_root / "config" / "agentic-os.yml",
        repo_root / ".qualitycat" / "agentic-os.yml",
    ]
    source: Optional[Path] = None
    for candidate in candidates:
        if candidate.exists():
            source = candidate
            break
    if source is None:
        return None, None, "no config file found at config/ or .qualitycat/"
    if not redact:
        try:
            data = source.read_bytes()
        except OSError as exc:
            return None, str(source.relative_to(repo_root)), (
                f"failed to read config: {exc.__class__.__name__}: {exc}"
            )
        return data, str(source.relative_to(repo_root)), "verbatim (no redaction)"
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return None, str(source.relative_to(repo_root)), (
            "PyYAML not installed in this environment; "
            "config not embedded to avoid leaking unredacted text"
        )
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — config may be malformed
        return None, str(source.relative_to(repo_root)), (
            f"failed to parse config as YAML: {exc.__class__.__name__}: {exc}"
        )
    redacted = redact_config(raw)
    body = yaml.safe_dump(redacted, sort_keys=False, default_flow_style=False)
    return body.encode("utf-8"), str(source.relative_to(repo_root)), None


def _doctor_json_bytes(repo_root: Path) -> bytes:
    """Inline import to avoid a CLI -> support-bundle import cycle.

    `build_doctor_payload` runs the full diagnostic (sut + models + docker)
    because the bundle's whole job is "give the maintainer enough context
    to triage without round-trips".
    """
    from .cli import build_doctor_payload

    payload = build_doctor_payload(
        repo_root,
        include_sut=True,
        include_models=True,
        include_docker=True,
        model_smoke_timeout_seconds=2,
    )
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")


def _gather_events(paths: RuntimePaths) -> List[Tuple[str, bytes, bool, int]]:
    out: List[Tuple[str, bytes, bool, int]] = []
    events_dir = paths.events_dir
    if not events_dir.exists():
        return out
    for entry in sorted(events_dir.iterdir()):
        if not entry.is_file() or entry.suffix != ".jsonl":
            continue
        try:
            data, truncated, original = _tail_jsonl_lines(entry)
        except OSError:
            continue
        out.append((entry.name, data, truncated, original))
    return out


def _gather_last_run(paths: RuntimePaths) -> List[Tuple[str, bytes, bool, int]]:
    """Pick the most recent run directory under runs/ and bundle its
    manifest + every small file."""
    runs_root = paths.runtime_root / "runs"
    if not runs_root.exists():
        return []
    run_dirs = [p for p in runs_root.iterdir() if p.is_dir()]
    if not run_dirs:
        return []
    # mtime ordering is intentionally simple — operators want "what just ran".
    run_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    latest = run_dirs[0]
    out: List[Tuple[str, bytes, bool, int]] = []
    for child in sorted(latest.rglob("*")):
        if not child.is_file():
            continue
        rel = child.relative_to(latest)
        try:
            data, truncated, original = _read_capped(child)
        except OSError:
            continue
        out.append((f"{latest.name}/{rel.as_posix()}", data, truncated, original))
    return out


def _gather_bugs(repo_root: Path) -> List[Tuple[str, bytes, bool, int]]:
    bugs_dir = repo_root / "bugs"
    if not bugs_dir.exists():
        return []
    out: List[Tuple[str, bytes, bool, int]] = []
    for entry in sorted(bugs_dir.iterdir()):
        if not entry.is_file():
            continue
        if entry.suffix not in {".json", ".md"}:
            continue
        try:
            data, truncated, original = _read_capped(entry)
        except OSError:
            continue
        out.append((entry.name, data, truncated, original))
    return out


def _tar_add_bytes(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    tar.addfile(info, io.BytesIO(data))


def _resolve_subsystems(
    include: Optional[set[str]], exclude: Optional[set[str]]
) -> set[str]:
    """Resolve `--include` / `--exclude` into the final enabled set.

    Validates names against SUPPORT_BUNDLE_SUBSYSTEMS; raises ValueError on
    unknown names or when both options are passed at once."""
    if include is not None and exclude is not None:
        raise ValueError("include and exclude are mutually exclusive")
    if include is not None:
        unknown = include - SUPPORT_BUNDLE_SUBSYSTEMS
        if unknown:
            raise ValueError(
                f"unknown subsystem(s) in include: {sorted(unknown)}; "
                f"valid: {sorted(SUPPORT_BUNDLE_SUBSYSTEMS)}"
            )
        return set(include)
    enabled = set(SUPPORT_BUNDLE_SUBSYSTEMS)
    if exclude is not None:
        unknown = exclude - SUPPORT_BUNDLE_SUBSYSTEMS
        if unknown:
            raise ValueError(
                f"unknown subsystem(s) in exclude: {sorted(unknown)}; "
                f"valid: {sorted(SUPPORT_BUNDLE_SUBSYSTEMS)}"
            )
        enabled -= exclude
    return enabled


def build_support_bundle(
    repo_root: Path,
    paths: RuntimePaths,
    *,
    dest: Optional[Path] = None,
    include: Optional[set[str]] = None,
    exclude: Optional[set[str]] = None,
    redact: bool = True,
    tag: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build the tarball on disk and return a JSON-friendly summary.

    Caller is responsible for surfacing the disclaimer; this function only
    writes bytes. By default the output lives under
    ``<runtime_root>/support-bundles/<ts>.tar.gz`` so it follows the same
    runtime-root contract as everything else. `dest` overrides the output
    directory. `tag` appends a suffix to the filename for easier sharing
    of multiple bundles from the same session. `include`/`exclude` filter
    the subsystems gathered (`config`, `doctor`, `events`, `runs`,
    `bugs`). `redact=False` embeds the config file verbatim — only use
    when the operator owns the destination of the bundle.
    """
    if tag is not None and not _TAG_RE.match(tag):
        raise ValueError(
            f"tag {tag!r} must match {_TAG_RE.pattern} so it is safe in a filename"
        )
    enabled = _resolve_subsystems(include, exclude)
    bundle_dir = dest if dest is not None else paths.runtime_root / SUPPORT_BUNDLE_DIRNAME
    bundle_dir.mkdir(parents=True, exist_ok=True)
    filename = _bundle_filename(now, tag=tag)
    out_path = bundle_dir / filename

    bundled: List[_BundledFile] = []

    cfg_bytes, cfg_source, cfg_note = (
        _config_bundle_bytes(repo_root, redact=redact)
        if "config" in enabled
        else (None, None, None)
    )
    doctor_bytes = _doctor_json_bytes(repo_root) if "doctor" in enabled else None
    events = _gather_events(paths) if "events" in enabled else []
    last_run = _gather_last_run(paths) if "runs" in enabled else []
    bugs = _gather_bugs(repo_root) if "bugs" in enabled else []

    with tarfile.open(out_path, mode="w:gz") as tar:
        if cfg_bytes is not None:
            _tar_add_bytes(tar, "config/agentic-os.yml", cfg_bytes)
            bundled.append(_BundledFile(
                arcname="config/agentic-os.yml",
                source=cfg_source,
                bytes_in_bundle=len(cfg_bytes),
                original_bytes=None,
                truncated=False,
                note=(
                    "redacted by leaf-key denylist"
                    if redact
                    else "VERBATIM — secrets NOT redacted (--no-redact)"
                ),
            ))
        elif cfg_note:
            note_bytes = (cfg_note + "\n").encode("utf-8")
            _tar_add_bytes(tar, "config/MISSING.txt", note_bytes)
            bundled.append(_BundledFile(
                arcname="config/MISSING.txt",
                source=cfg_source,
                bytes_in_bundle=len(note_bytes),
                original_bytes=None,
                truncated=False,
                note=cfg_note,
            ))

        if doctor_bytes is not None:
            _tar_add_bytes(tar, "doctor.json", doctor_bytes)
            bundled.append(_BundledFile(
                arcname="doctor.json",
                source=None,
                bytes_in_bundle=len(doctor_bytes),
                original_bytes=None,
                truncated=False,
                note="agentic-os doctor --sut --models --docker",
            ))

        for name, data, truncated, original in events:
            arcname = f"events/{name}"
            _tar_add_bytes(tar, arcname, data)
            bundled.append(_BundledFile(
                arcname=arcname,
                source=str((paths.events_dir / name).relative_to(repo_root)),
                bytes_in_bundle=len(data),
                original_bytes=original,
                truncated=truncated,
                note=f"tail of last {_EVENT_TAIL_LINES} lines" if truncated else None,
            ))

        for rel, data, truncated, original in last_run:
            arcname = f"runs/{rel}"
            _tar_add_bytes(tar, arcname, data)
            bundled.append(_BundledFile(
                arcname=arcname,
                source=None,
                bytes_in_bundle=len(data),
                original_bytes=original,
                truncated=truncated,
                note=(
                    f"capped at {_PER_FILE_BYTE_CAP} bytes; original was {original} bytes"
                    if truncated
                    else None
                ),
            ))

        for name, data, truncated, original in bugs:
            arcname = f"bugs/{name}"
            _tar_add_bytes(tar, arcname, data)
            bundled.append(_BundledFile(
                arcname=arcname,
                source=str((repo_root / "bugs" / name).relative_to(repo_root)),
                bytes_in_bundle=len(data),
                original_bytes=original,
                truncated=truncated,
                note=(
                    f"capped at {_PER_FILE_BYTE_CAP} bytes; original was {original} bytes"
                    if truncated
                    else None
                ),
            ))

        manifest = {
            "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
            "repo_root": str(repo_root),
            "runtime_root": str(paths.runtime_root.relative_to(repo_root)),
            "subsystems_enabled": sorted(enabled),
            "redaction_policy": (
                f"Leaf keys matching the secret denylist are replaced with "
                f"'{_REDACT_PLACEHOLDER}'. Review the bundle contents before "
                "sharing — the denylist is conservative, not exhaustive."
            ) if redact else (
                "REDACTION DISABLED (--no-redact). The config file is "
                "embedded VERBATIM. Treat this bundle like any other secret."
            ),
            "redacted": redact,
            "tag": tag,
            "per_file_byte_cap": _PER_FILE_BYTE_CAP,
            "event_tail_lines": _EVENT_TAIL_LINES,
            "files": [
                {
                    "arcname": f.arcname,
                    "source": f.source,
                    "bytes_in_bundle": f.bytes_in_bundle,
                    "original_bytes": f.original_bytes,
                    "truncated": f.truncated,
                    "note": f.note,
                }
                for f in bundled
            ],
        }
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        _tar_add_bytes(tar, "MANIFEST.json", manifest_bytes)

    total_size = out_path.stat().st_size
    # `path` stays relative to repo_root when the bundle lives under it
    # (the dashboard's /files static route needs that). With `--dest`
    # outside the repo, fall back to the absolute path.
    try:
        rel_path = str(out_path.relative_to(repo_root))
    except ValueError:
        rel_path = str(out_path)
    return {
        "path": rel_path,
        "absolute_path": str(out_path),
        "bytes": total_size,
        "filename": filename,
        "manifest": manifest,
        "disclaimer": (
            "Review the bundle before sending. Config secrets are redacted "
            "by a conservative denylist; other operator data (events, run "
            "artifacts, bug notes) is included verbatim."
        ) if redact else (
            "REDACTION DISABLED. The config file is embedded verbatim and "
            "may contain credentials. Treat the bundle like any other secret."
        ),
    }
