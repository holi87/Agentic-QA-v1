"""Issue #270 — structured reasoning transcript per model invocation.

`step.progress` shows WHAT happened; the transcript shows WHY — the
chain-of-thought, tool calls and model output text behind a step. Stored in
`model_transcripts`, correlated to the orchestration step via the
`transcript.chunk` event (which carries both step_id and invocation_id).

Capture is opt-in by cost: `autonomy.transcript_capture` is one of
`always | on_block | never` (default `on_block`). `never` is zero-overhead —
`capture_transcript` returns on its first line without reading the envelope,
building chunks, or touching the DB.

Payloads pass the secret-redaction filter before they are persisted: the row
is the wire format that leaves the process, so the redaction happens at write.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Dict, List, Optional, Tuple

from .time_utils import now_iso

_TRANSCRIPT_KINDS = ("thinking", "tool_call", "tool_result", "text", "error")


def _default_redactor(text: str) -> str:
    from .models import redact_prompt

    return redact_prompt(text)


def extract_chunks(envelope: Any, stdout_text: str) -> List[Tuple[str, str]]:
    """Return ordered (kind, payload) chunks.

    Structured elements are read from `envelope.metadata` when a provider
    exposes them (thinking / tool_calls / tool_results). Today's envelope
    schema keeps `metadata` free-form, so in practice this falls back to a
    single `text` chunk built from the envelope body (or raw stdout) — the
    honest text-only path the acceptance calls for when structure is absent.
    """
    chunks: List[Tuple[str, str]] = []
    meta = getattr(envelope, "metadata", None) or {}
    if isinstance(meta, dict):
        thinking = meta.get("thinking")
        if isinstance(thinking, str) and thinking.strip():
            chunks.append(("thinking", thinking))
        for tc in meta.get("tool_calls") or []:
            chunks.append(("tool_call", tc if isinstance(tc, str) else json.dumps(tc, sort_keys=True)))
        for tr in meta.get("tool_results") or []:
            chunks.append(("tool_result", tr if isinstance(tr, str) else json.dumps(tr, sort_keys=True)))
    body = (getattr(envelope, "body", "") or "") if envelope is not None else ""
    text = body.strip() or (stdout_text or "").strip()
    if text:
        chunks.append(("text", text))
    return chunks


def _is_eligible(mode: str, outcome: str) -> bool:
    if mode == "always":
        return True
    if mode == "on_block":
        # At the model-invocation layer the outcome is ok|failed; "blocked" is
        # a higher-layer step outcome and cannot occur here. Capture on failure.
        return outcome in ("failed", "blocked")
    return False


def capture_transcript(
    conn: sqlite3.Connection,
    events: Any,
    *,
    mode: str,
    outcome: str,
    invocation_id: str,
    step_id: Optional[str],
    envelope: Any,
    stdout_text: str,
    redactor: Optional[Callable[[str], str]] = None,
) -> int:
    """Persist + emit the transcript for one invocation. Returns chunk count."""
    if mode == "never":
        # Zero-overhead: no metadata read, no chunk build, no DB/redaction.
        return 0
    if not _is_eligible(mode, outcome):
        return 0
    redact = redactor or _default_redactor
    chunks = extract_chunks(envelope, stdout_text)
    written = 0
    for ord_, (kind, payload) in enumerate(chunks):
        safe = redact(payload)
        ts = now_iso()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO model_transcripts(invocation_id, kind, ord, payload, ts) "
                "VALUES (?, ?, ?, ?, ?);",
                (invocation_id, kind, ord_, safe, ts),
            )
        except sqlite3.Error:
            continue
        written += 1
        try:
            events.write(
                "transcript.chunk",
                payload={
                    "invocation_id": invocation_id,
                    "step_id": step_id,
                    "kind": kind,
                    "ord": ord_,
                },
            )
        except Exception:  # pragma: no cover - event emit must not break capture
            pass
    return written


def get_transcript(conn: sqlite3.Connection, invocation_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT invocation_id, kind, ord, payload, ts FROM model_transcripts "
        "WHERE invocation_id=? ORDER BY ord ASC;",
        (invocation_id,),
    ).fetchall()
    return [
        {"invocation_id": r["invocation_id"], "kind": r["kind"], "ord": r["ord"],
         "payload": r["payload"], "ts": r["ts"]}
        for r in rows
    ]
