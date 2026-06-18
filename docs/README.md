# docs/

Status: active

Operator-facing and reference documentation for Agentic OS.

## Status convention

Every Markdown file under `docs/` declares its lifecycle status as the
first metadata block after the H1, using the following keys:

```markdown
# <title>

Status: active | superseded | draft
Superseded-on: <YYYY-MM-DD>      # only when status is "superseded"
Superseded-by: <relative path>   # optional pointer to the replacement
Supersedes:    <relative path>   # optional, on the replacement
Reason: <one-line motivation>    # required for "superseded" and "draft"
```

Meaning:

- **active** — the document reflects the current state of `main`. Operators
  may rely on it for decisions and runbooks.
- **superseded** — the document is kept for historical context only.
  Decisions must NOT be made from a superseded document; consult its
  `Superseded-by:` pointer (or the issue tracker / `README.md` Status
  section) instead.
- **draft** — under active editing; content may contradict the live code.
  Treat as a working note.

A document with no status header is implicitly **active**. Contract
documents (cli-contract, runtime-contract, database-schema) carry a
secondary `Contract gate:` field that records their acceptance state
(separate from lifecycle).

## Why we annotate instead of deleting

Point-in-time analyses (RC readiness reports, remediation plans, audit
write-ups) are useful as history even after the underlying gaps are
closed. Deleting them loses the audit trail; leaving them un-annotated
misleads operators who land on the file via search. The header is the
minimum signal that prevents stale documents from looking authoritative.
