"""Issue #232 — autonomous triage classifier.

Pure-function classifier that decides, per failed test, which of the
four autonomous actions the triager should take when
`autonomy.triage_batch=true`:

- `append_known_bug`     — fingerprint matches an existing
  `bugs/BUG-NNN-*.md`; reviewer tags the scenario `@known-bug @bug-NNN`.
- `auto_create_bug`      — high-confidence classification; the triager
  creates a new bug carrying `auto_classified: true` in its frontmatter.
- `queue_operator`       — confidence too low; loop continues but the
  failure waits for an operator YES/NO decision (existing behavior).
- `skip_infra`           — infrastructure failure (port / DNS / SUT
  down); never opens a bug, emits a re-run request.

The workflow caller is expected to feed each failure through
`classify_failure` and execute the returned action. Fingerprint
matching uses Levenshtein distance ≤ 5 over the concatenated
status + endpoint + assertion text per the issue spec.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class TriageDecision:
    action: str           # "append_known_bug" | "auto_create_bug" | "queue_operator" | "skip_infra"
    rule: str             # short rule id (e.g. "fingerprint:BUG-014", "owasp:api1")
    severity: Optional[str] = None  # for auto_create_bug
    priority: Optional[str] = None  # for auto_create_bug
    bug_id: Optional[str] = None    # for append_known_bug
    reason: str = ""
    tags_to_append: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class KnownBug:
    bug_id: str           # "BUG-014-payment-fails"
    status: Optional[int]
    endpoint: Optional[str]
    assertion_text: str   # canonical assertion text from bugs/<id>.md


# --- Public API ------------------------------------------------------------

def classify_failure(
    failure: Dict[str, Any],
    *,
    known_bugs: Sequence[KnownBug] = (),
) -> TriageDecision:
    """Return the triage decision for one failure."""
    # Rule 1: infra failure short-circuits everything.
    infra_rule = _detect_infra(failure)
    if infra_rule:
        return TriageDecision(
            action="skip_infra",
            rule=infra_rule,
            reason="Infrastructure failure — re-run requested, no bug filed.",
        )

    # Rule 2: fingerprint match against existing bugs.
    match = _match_known_bug(failure, known_bugs)
    if match is not None:
        bug_id, distance = match
        scenario_tag = _bug_scenario_tag(bug_id)
        return TriageDecision(
            action="append_known_bug",
            rule=f"fingerprint:{bug_id}:dist={distance}",
            bug_id=bug_id,
            reason=f"Fingerprint matches {bug_id} (Levenshtein={distance}).",
            tags_to_append=("@known-bug", scenario_tag),
        )

    # Rule 3: unambiguous OWASP classification → S1/P1.
    owasp_rule = _detect_owasp_unambiguous(failure)
    if owasp_rule:
        return TriageDecision(
            action="auto_create_bug",
            rule=owasp_rule,
            severity="S1",
            priority="P1",
            reason=(
                "Security failure with unambiguous OWASP mapping "
                f"({owasp_rule}); auto-filed at S1/P1 pending operator audit."
            ),
        )

    # Rule 4: spec-mirroring deterministic failure ≤ S2.
    severity = _recommend_severity(failure)
    if severity in {"S2", "S3", "S4"} and _has_spec_citation(failure) and _looks_deterministic(failure):
        return TriageDecision(
            action="auto_create_bug",
            rule=f"deterministic-spec-mirror:{severity}",
            severity=severity,
            priority=_default_priority(severity),
            reason=(
                f"Spec citation present; deterministic failure ({severity}). "
                "Operator may downgrade via dashboard; loop continues."
            ),
        )

    # Default: queue for operator decision.
    return TriageDecision(
        action="queue_operator",
        rule="low-confidence",
        reason="Confidence too low for autonomous classification.",
    )


# --- Heuristics ------------------------------------------------------------

_INFRA_SIGNS = (
    re.compile(r"ECONNREFUSED|ENOTFOUND|EAI_AGAIN", re.IGNORECASE),
    re.compile(r"connection refused|name resolution|temporary failure", re.IGNORECASE),
    re.compile(r"port\s+\d+\s+(?:is\s+)?(?:busy|in use|closed)", re.IGNORECASE),
    re.compile(r"docker.*?not\s+running|container.*?exited", re.IGNORECASE),
    re.compile(r"\bDNS\b.*?(?:fail|unreachable)", re.IGNORECASE),
)


def _detect_infra(failure: Dict[str, Any]) -> Optional[str]:
    haystack = " ".join(
        str(failure.get(k) or "") for k in
        ("error_message", "stack_head", "stderr", "stdout")
    )
    for pat in _INFRA_SIGNS:
        m = pat.search(haystack)
        if m:
            return f"infra:{m.group(0).strip().lower().replace(' ', '-')[:40]}"
    return None


def _match_known_bug(
    failure: Dict[str, Any], known_bugs: Sequence[KnownBug]
) -> Optional[tuple[str, int]]:
    if not known_bugs:
        return None
    fp = _fingerprint(failure)
    best: Optional[tuple[str, int]] = None
    for kb in known_bugs:
        kb_fp = _known_bug_fingerprint(kb)
        if not kb_fp:
            continue
        distance = _levenshtein(fp, kb_fp)
        if distance <= 5 and (best is None or distance < best[1]):
            best = (kb.bug_id, distance)
    return best


def _fingerprint(failure: Dict[str, Any]) -> str:
    status = failure.get("status_code") or _extract_status_from_text(
        " ".join(str(failure.get(k) or "") for k in ("error_message", "stack_head"))
    )
    endpoint = failure.get("endpoint") or _extract_endpoint(
        " ".join(str(failure.get(k) or "") for k in ("error_message", "stack_head", "name"))
    )
    assertion = _normalize(str(failure.get("error_message") or ""))[:80]
    return f"{status or ''}|{endpoint or ''}|{assertion}"


def _known_bug_fingerprint(kb: KnownBug) -> str:
    return f"{kb.status or ''}|{kb.endpoint or ''}|{_normalize(kb.assertion_text)[:80]}"


_OWASP_PATTERNS = (
    # @owasp-api1 (BOLA) + status 200 from unauthorized call → S1/P1.
    (re.compile(r"@owasp-api1\b", re.IGNORECASE), "200", "owasp:api1-bola"),
    (re.compile(r"@owasp-api2\b", re.IGNORECASE), "200", "owasp:api2-broken-auth"),
    (re.compile(r"@owasp-api5\b", re.IGNORECASE), "200", "owasp:api5-bfla"),
    (re.compile(r"@owasp-api8\b", re.IGNORECASE), "200", "owasp:api8-misconfig"),
)


def _detect_owasp_unambiguous(failure: Dict[str, Any]) -> Optional[str]:
    tags = " ".join(str(t) for t in (failure.get("tags") or []))
    status = str(
        failure.get("status_code")
        or _extract_status_from_text(str(failure.get("error_message") or ""))
        or ""
    )
    for pat, expected_status, rule in _OWASP_PATTERNS:
        if pat.search(tags) and status == expected_status:
            return rule
    return None


_SEVERITY_HINTS = (
    (re.compile(r"@s1\b|critical|outage|data loss", re.IGNORECASE), "S1"),
    (re.compile(r"@s2\b|major|broken flow", re.IGNORECASE), "S2"),
    (re.compile(r"@s3\b|minor|cosmetic", re.IGNORECASE), "S3"),
    (re.compile(r"@s4\b|trivial", re.IGNORECASE), "S4"),
)


def _recommend_severity(failure: Dict[str, Any]) -> Optional[str]:
    haystack = " ".join(
        str(failure.get(k) or "") for k in ("tags", "name", "error_message")
    )
    for pat, sev in _SEVERITY_HINTS:
        if pat.search(haystack):
            return sev
    return "S3"  # safe default — minor severity


def _default_priority(severity: Optional[str]) -> str:
    return {"S1": "P1", "S2": "P2", "S3": "P3", "S4": "P4"}.get(
        severity or "", "P3"
    )


def _has_spec_citation(failure: Dict[str, Any]) -> bool:
    return bool(failure.get("feature_uri") or failure.get("spec_source"))


def _looks_deterministic(failure: Dict[str, Any]) -> bool:
    runs = failure.get("run_count")
    if isinstance(runs, int) and runs >= 2:
        return True
    if failure.get("deterministic") is True:
        return True
    # Single-run failures are NOT auto-classified deterministic unless
    # explicit; the issue spec wants ≥ 2 runs.
    return False


def _bug_scenario_tag(bug_id: str) -> str:
    m = re.match(r"BUG-(\d+)", bug_id)
    return f"@bug-{int(m.group(1))}" if m else f"@{bug_id.lower()}"


def _extract_status_from_text(text: str) -> Optional[int]:
    m = re.search(r"\bHTTP\s*(\d{3})\b", text, re.IGNORECASE) or re.search(
        r"\bstatus\s*[:=]?\s*(\d{3})\b", text, re.IGNORECASE
    )
    return int(m.group(1)) if m else None


def _extract_endpoint(text: str) -> Optional[str]:
    m = re.search(r"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(/\S+)", text)
    return f"{m.group(1)} {m.group(2)}" if m else None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(
                curr[j - 1] + 1,        # insert
                prev[j] + 1,            # delete
                prev[j - 1] + cost,     # substitute
            )
        prev = curr
    return prev[-1]


# --- Bugs index helper -----------------------------------------------------

def load_known_bugs(bugs_dir: "os.PathLike[str]" | str) -> List[KnownBug]:
    """Scan bugs/ for fingerprint data — best-effort frontmatter parse."""
    import os
    from pathlib import Path

    root = Path(bugs_dir)
    if not root.exists():
        return []
    out: List[KnownBug] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_file() or not entry.name.startswith("BUG-"):
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = _parse_frontmatter(text)
        body = _strip_frontmatter(text)
        out.append(
            KnownBug(
                bug_id=entry.stem,
                status=_extract_status_from_text(body),
                endpoint=_extract_endpoint(body),
                assertion_text=meta.get("scenario") or body[:200],
            )
        )
    return out


def _parse_frontmatter(text: str) -> Dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    block = text[4:end]
    out: Dict[str, str] = {}
    for line in block.splitlines():
        m = re.match(r"^([a-zA-Z_]+)\s*:\s*(.+?)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 4)
    if end < 0:
        return text
    return text[end + 4 :]


def triage_batch_enabled(repo_root: "os.PathLike[str]" | str) -> bool:
    """Read `autonomy.triage_batch` from the active config — defaults off."""
    try:
        from pathlib import Path

        from .config import load_or_default

        cfg = load_or_default(Path(repo_root))
        autonomy = cfg.raw.get("autonomy") or {}
        return bool(autonomy.get("triage_batch", False))
    except Exception:
        return False
