"""Filesystem helpers and markdown/line text utilities.

Split from inbox.py (issue #292).
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from ..paths import RuntimePaths


def _move_with_timestamp(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = dest_dir / f"{src.stem}-{ts}{src.suffix}"
    counter = 2
    while dest.exists():
        dest = dest.with_name(f"{src.stem}-{ts}-{counter}{src.suffix}")
        counter += 1
    shutil.move(str(src), str(dest))
    return dest


def _quarantine_failure(
    src: Path, failed_dir: Path, paths: RuntimePaths, *, error: str
) -> Path:
    dest = _move_with_timestamp(src, failed_dir)
    sidecar = dest.with_name(dest.name + ".error.txt")
    sidecar.write_text(error + "\n", encoding="utf-8")
    return dest


def _rel(paths: RuntimePaths, path: Path) -> str:
    try:
        return str(path.relative_to(paths.repo_root))
    except ValueError:
        return str(path)


def _first_h1(text: str) -> Optional[str]:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            return stripped[2:].strip()
        return None
    return None


def _first_nonempty(text: str) -> Optional[str]:
    for line in text.splitlines():
        s = line.strip()
        if s:
            return s
    return None


def _clean_markdown_line(raw: str) -> str:
    line = raw.strip()
    line = re.sub(r"^#{1,6}\s+", "", line)
    line = re.sub(r"^[-*]\s+", "", line)
    line = re.sub(r"^\d+[\.)]\s+", "", line)
    return line.strip()


def _is_metadata_line(line: str) -> bool:
    return bool(re.match(r"^(priority|sut root)\s*:", line, flags=re.IGNORECASE))


def _markdown_list_or_default(items: List[str], default: str) -> str:
    if not items:
        return f"- {default}"
    return "\n".join(f"- {item}" for item in items)


def _highest_priority(values: Any) -> str:
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    best = _DEFAULT_PRIORITY
    for value in values:
        normalized = str(value).strip().upper()
        if normalized in order and order[normalized] < order[best]:
            best = normalized
    return best


def _dedupe(values: List[str]) -> List[str]:
    seen = set()
    out = []
    for value in values:
        key = value.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(value.strip())
    return out


def _clip(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


_DEFAULT_PRIORITY = "P2"
