"""executable API test generator.

Default target: Playwright TypeScript `APIRequestContext`. Output is a
ready-to-run `.spec.ts` file with:

- source-ref comment(s) at the top (auditable lineage from spec/docs);
- exact assertion text carried over from the plan (no trivial `status<500`);
- env-var or file-ref credentials (never literal secret strings);
- cleanup hook when the endpoint mutates state;
- deterministic title and file name derived from `candidate_id` + slug.

The generator never writes to the SUT tree directly — output is collected
into a patch artifact under `agentic-os-runtime/patches/<task>/<run>/`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..errors import UsageError
from ..plan_v2 import PlanItem
from ._escape import js_comment_text, js_str


@dataclass(frozen=True)
class GeneratedTest:
    candidate_id: str
    relative_path: str          # path inside SUT/test repo, e.g. tests/api/...
    content: str                # full file body
    runner: str = "playwright-ts"


def generate_api_test(
    item: PlanItem,
    *,
    tests_dir: str = "tests",
    api_base_url_env: str = "API_BASE_URL",
    credentials_env: Optional[str] = None,
    coverage_floor: bool = False,
) -> GeneratedTest:
    """Render a Playwright TS spec for one PlanItem."""
    if item.test_type != "api":
        raise UsageError(f"generate_api_test: only test_type='api' supported, got {item.test_type!r}")
    if item.decision != "generate_now":
        raise UsageError(
            f"generate_api_test: only decision='generate_now' supported, got {item.decision!r}"
        )
    if not item.source_refs:
        raise UsageError("generate_api_test requires at least one source_ref")
    if not item.expected_assertion.strip():
        raise UsageError("generate_api_test requires expected_assertion")
    method = (item.target_method or "").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        raise UsageError(f"unsupported target_method: {item.target_method!r}")
    if not item.target_path:
        raise UsageError("generate_api_test requires target_path")

    slug = _slug(item.title) or "case"
    file_name = f"{item.candidate_id.lower()}-{slug}.spec.ts"
    rel_path = f"{tests_dir.rstrip('/')}/api/{file_name}"

    body = _render_playwright_spec(
        item=item,
        method=method,
        api_base_url_env=api_base_url_env,
        credentials_env=credentials_env,
        coverage_floor=coverage_floor,
    )
    return GeneratedTest(
        candidate_id=item.candidate_id,
        relative_path=rel_path,
        content=body,
    )


def generate_api_tests(
    items: Iterable[PlanItem],
    *,
    tests_dir: str = "tests",
    api_base_url_env: str = "API_BASE_URL",
    credentials_env: Optional[str] = None,
    coverage_floor: bool = False,
) -> List[GeneratedTest]:
    """Generate all api-type generate_now items. Other items skipped silently —
    the plan gate already enforced their state."""
    out: List[GeneratedTest] = []
    for item in items:
        if item.test_type != "api" or item.decision != "generate_now":
            continue
        out.append(
            generate_api_test(
                item,
                tests_dir=tests_dir,
                api_base_url_env=api_base_url_env,
                credentials_env=credentials_env,
                coverage_floor=coverage_floor,
            )
        )
    return out


def write_patch_artifact(
    tests: Iterable[GeneratedTest],
    *,
    output_dir: Path,
) -> Dict[str, object]:
    """Write each generated test under output_dir/files/<rel_path> and a
    manifest.json describing the bundle. Output_dir is typically
    `agentic-os-runtime/patches/<task>/<run>/`.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    files_dir = output_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    entries: List[Dict[str, str]] = []
    for gen in tests:
        target = files_dir / gen.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(gen.content, encoding="utf-8")
        entries.append(
            {
                "candidate_id": gen.candidate_id,
                "relative_path": gen.relative_path,
                "runner": gen.runner,
            }
        )
    manifest = {
        "version": "1.0",
        "kind": "api-tests-patch",
        "files": entries,
    }
    import json as _json

    (output_dir / "manifest.json").write_text(
        _json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest


def _slug(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:60]


def _render_playwright_spec(
    *,
    item: PlanItem,
    method: str,
    api_base_url_env: str,
    credentials_env: Optional[str],
    coverage_floor: bool = False,
) -> str:
    refs = "\n".join(f" *   - {js_comment_text(ref)}" for ref in item.source_refs)
    bug_comment = ""
    if item.known_bug_relation:
        bug_comment = f" *\n * Known bug: {js_comment_text(item.known_bug_relation)}\n"
    cleanup_block = _render_cleanup(item, method)
    auth_block = _render_auth(credentials_env)
    body_block = _render_body(item, method)
    expected = js_comment_text(item.expected_assertion)
    test_title = f"{item.candidate_id} — {item.title}"
    companions = _render_api_companions(
        item=item,
        method=method,
        credentials_env=credentials_env,
        coverage_floor=coverage_floor,
    )
    return f"""// AUTO-GENERATED by Agentic OS API generator.
// candidate: {js_comment_text(item.candidate_id)}
// generator: playwright-ts
//
/**
 * Sources:
{refs}
{bug_comment} *
 * Expected behavior (verbatim from TEST-PLAN):
 *   {expected}
 */

import {{ test, expect, request }} from '@playwright/test';

const API_BASE_URL = process.env[{js_str(api_base_url_env)}];
if (!API_BASE_URL) {{
  throw new Error({js_str(api_base_url_env + " env var is required to run this test")});
}}

test({js_str(test_title)}, async () => {{
  const ctx = await request.newContext({{ baseURL: API_BASE_URL{auth_block} }});
  try {{
    const response = await ctx.{method.lower()}({js_str(item.target_path)}{body_block});

    // Plan-derived expectation — DO NOT weaken without operator decision.
    // {expected}
    {_render_assertions(item)}
  }} finally {{
    {cleanup_block}
    await ctx.dispose();
  }}
}});
{companions}"""


# Issue #231 — API negative-coverage companions. Each companion is a
# separate `test()` block in the same file so a companion failure cannot
# mask the happy-path verdict. `test.skip(...)` guards prerequisites
# (env vars, OpenAPI schema, mutating method) so the file runs cleanly
# in any operator workspace. Each block carries a fixed marker comment
# (`// agentic-os:companion:<kind>`) for the #233 reviewer grep.
def _render_api_companions(
    *,
    item: PlanItem,
    method: str,
    credentials_env: Optional[str],
    coverage_floor: bool,
) -> str:
    if not coverage_floor:
        return ""
    notes = {n.strip() for n in (item.notes or [])}
    if "no-negative-companions" in notes:
        return ""
    blocks: List[str] = []
    path_literal = js_str(item.target_path)
    body_arg = _render_body(item, method)
    mutating = method in {"POST", "PUT", "PATCH", "DELETE"}

    # --- neg-auth: drop the Authorization header → expect 401/403.
    if credentials_env:
        blocks.append(
            f"""
test({js_str(item.candidate_id + ' — neg-auth')}, async () => {{
  // agentic-os:companion:neg-auth
  const _ctx = await request.newContext({{ baseURL: API_BASE_URL }});
  try {{
    const _resp = await _ctx.{method.lower()}({path_literal}{body_arg});
    expect([401, 403], 'SEC: unauthenticated call must be rejected').toContain(_resp.status());
  }} finally {{
    await _ctx.dispose();
  }}
}});
"""
        )

    # --- BOLA / IDOR canary: swap `{id}` with another tenant's id from env.
    bola_match = re.search(r"\{[^}]+\}|/(\d+)(?=/|$)", item.target_path or "")
    if bola_match and credentials_env:
        other_env = f"{credentials_env}_OTHER_ID"
        other_path = re.sub(r"\{[^}]+\}", "${_otherId}", item.target_path or "")
        other_path = re.sub(r"/(\d+)(?=/|$)", "/${_otherId}", other_path)
        path_template = "`" + other_path + "`"
        blocks.append(
            f"""
test({js_str(item.candidate_id + ' — bola')}, async () => {{
  // agentic-os:companion:bola
  const _otherId = process.env[{js_str(other_env)}];
  test.skip(!_otherId, {js_str(other_env + ' env var not set — BOLA canary skipped')});
  const _ctx = await request.newContext({{ baseURL: API_BASE_URL{_render_auth(credentials_env)} }});
  try {{
    const _resp = await _ctx.{method.lower()}({path_template}{body_arg});
    expect([403, 404], 'SEC: foreign-owned id must not return 200').toContain(_resp.status());
  }} finally {{
    await _ctx.dispose();
  }}
}});
"""
        )

    # --- Boundary: empty + oversized payload (mutating only).
    if mutating:
        blocks.append(
            f"""
test({js_str(item.candidate_id + ' — boundary-empty')}, async () => {{
  // agentic-os:companion:boundary
  const _ctx = await request.newContext({{ baseURL: API_BASE_URL{_render_auth(credentials_env)} }});
  try {{
    const _resp = await _ctx.{method.lower()}({path_literal}, {{ data: {{}} }});
    expect([400, 415, 422], 'BOUND: empty body must be rejected').toContain(_resp.status());
  }} finally {{
    await _ctx.dispose();
  }}
}});

test({js_str(item.candidate_id + ' — boundary-oversize')}, async () => {{
  // agentic-os:companion:boundary
  const _ctx = await request.newContext({{ baseURL: API_BASE_URL{_render_auth(credentials_env)} }});
  try {{
    const _big = {{ payload: 'x'.repeat(1_000_000) }};
    const _resp = await _ctx.{method.lower()}({path_literal}, {{ data: _big }});
    expect([400, 413, 422], 'BOUND: oversize payload must be rejected').toContain(_resp.status());
  }} finally {{
    await _ctx.dispose();
  }}
}});
"""
        )

    # --- Injection canary: SQLi + reflected XSS probes; status must not
    # be 500 and body must not echo the payload verbatim.
    if mutating:
        blocks.append(
            f"""
test({js_str(item.candidate_id + ' — injection-canary')}, async () => {{
  // agentic-os:companion:injection
  const _ctx = await request.newContext({{ baseURL: API_BASE_URL{_render_auth(credentials_env)} }});
  try {{
    const _probes = [
      {{ kind: 'sqli', value: "' OR 1=1 --" }},
      {{ kind: 'xss', value: '<script>x</script>' }},
    ];
    for (const _p of _probes) {{
      const _resp = await _ctx.{method.lower()}({path_literal}, {{ data: {{ q: _p.value }} }});
      expect(_resp.status(), `SEC: ${{_p.kind}} probe must not 500`).not.toBe(500);
      const _txt = await _resp.text();
      expect(_txt.includes(_p.value), `SEC: ${{_p.kind}} probe must not echo verbatim`).toBe(false);
    }}
  }} finally {{
    await _ctx.dispose();
  }}
}});
"""
        )

    # --- Schema validate: ajv import, fail-soft when dep absent. Always
    # emitted when coverage_floor is on so the marker contract holds.
    blocks.append(
        f"""
test({js_str(item.candidate_id + ' — schema-validate')}, async () => {{
  // agentic-os:companion:schema
  const _ctx = await request.newContext({{ baseURL: API_BASE_URL{_render_auth(credentials_env)} }});
  try {{
    const _resp = await _ctx.{method.lower()}({path_literal}{body_arg});
    try {{
      const {{ default: _Ajv }} = await import('ajv');
      const _schema = process.env[{js_str(item.candidate_id.upper().replace('-', '_') + '_RESPONSE_SCHEMA')}];
      test.skip(!_schema, 'response schema env var not set — schema-validate skipped');
      const _ajv = new _Ajv();
      const _validate = _ajv.compile(JSON.parse(_schema!));
      const _body = await _resp.json();
      expect.soft(_validate(_body), `SCHEMA: ${{JSON.stringify(_validate.errors)}}`).toBe(true);
    }} catch (e) {{
      /* ajv not installed — schema-validate companion skipped */
    }}
  }} finally {{
    await _ctx.dispose();
  }}
}});
"""
    )

    return "".join(blocks)


def _render_auth(credentials_env: Optional[str]) -> str:
    if not credentials_env:
        return ""
    return (
        ",\n      extraHTTPHeaders: { 'Authorization': `Bearer ${process.env["
        + js_str(credentials_env)
        + "] ?? ''}` }"
    )


def _render_body(item: PlanItem, method: str) -> str:
    if method in {"GET", "DELETE", "HEAD"}:
        return ""
    data = (item.required_test_data or "").strip()
    if not data:
        return ""
    # Issue #94 — refuse to invent request bodies. Mutating API tests
    # must carry parseable JSON, not free-text descriptions wrapped as
    # `{"note": "..."}`.
    if not (data.startswith("{") or data.startswith("[")):
        raise UsageError(
            f"api generator: required_test_data for {item.candidate_id} "
            f"({method}) must be JSON object or array, got free text. "
            "Approve the candidate with `--test-data '{...}'` or update "
            "the plan with a JSON payload."
        )
    try:
        import json as _json

        _json.loads(data)
    except Exception as exc:
        raise UsageError(
            f"api generator: required_test_data for {item.candidate_id} "
            f"is not valid JSON: {exc}"
        ) from exc
    return ", { data: JSON.parse(" + js_str(data) + ") }"


def _render_cleanup(item: PlanItem, method: str) -> str:
    # Issue #91 — mutating tests must produce executable cleanup or
    # block generation. Read-only methods stay no-op.
    if method in {"GET", "HEAD"}:
        return "// no-op cleanup for read-only method"
    raw = (item.cleanup_strategy or "").strip()
    if not raw:
        raise UsageError(
            f"api generator: cleanup_strategy required for mutating "
            f"{method} on {item.candidate_id}"
        )
    marker = raw.lower()
    no_teardown_markers = (
        "read-only",
        "rolled back in test body",
        "rolled back",
        "none",
        "rejection",
        "no resource",
        "no cleanup",
        "no teardown",
    )
    if any(m in marker for m in no_teardown_markers):
        return f"// cleanup: {js_comment_text(raw)} — no teardown call emitted by design"
    cleanup_match = re.match(
        r"^(DELETE|POST|PUT|PATCH)\s+(/\S+)", raw, re.IGNORECASE
    )
    if cleanup_match:
        cleanup_method = cleanup_match.group(1).lower()
        cleanup_path = cleanup_match.group(2)
        comment = js_comment_text(raw)
        return (
            f"// cleanup from plan: {comment}\n"
            "    try {\n"
            f"      await ctx.{cleanup_method}({js_str(cleanup_path)});\n"
            "    } catch (cleanupErr) {\n"
            "      // Cleanup failure is logged, not propagated — primary\n"
            "      // assertion already drives the test verdict.\n"
            "      console.warn('cleanup failed:', cleanupErr);\n"
            "    }"
        )
    raise UsageError(
        f"api generator: cleanup_strategy for {item.candidate_id} must "
        "be one of: 'read-only', 'rolled back in test body', or "
        "'<METHOD> <PATH>' (e.g. 'DELETE /orders/{id}'). "
        f"got: {raw!r}"
    )


def _render_assertions(item: PlanItem) -> str:
    """Issue #95 — convert structured expectations into executable
    assertions instead of silently dropping them. The plan may name a
    status code, a body field expectation, a header expectation, or
    body presence (`body.<field> present`).

    Unsupported assertion text raises `UsageError` so a plan that asked
    for body/header behavior cannot become a status-only test."""
    expected = item.expected_assertion
    status_match = re.search(r"\bHTTP\s*(\d{3})\b", expected, re.IGNORECASE) or re.search(
        r"\bstatus(?:_code|\s+code)?\s*[:=]?\s*(\d{3})\b", expected, re.IGNORECASE
    )
    lines: List[str] = []
    if status_match:
        lines.append(f"expect(response.status()).toBe({status_match.group(1)});")
    else:
        raise UsageError(
            f"api generator requires explicit HTTP status in expected_assertion for {item.candidate_id}"
        )

    body_used = False
    # error.code = X (legacy)
    body_key = re.search(r"error\.code\s*=\s*([A-Za-z0-9_]+)", expected)
    if body_key:
        if not body_used:
            lines.append("const body = await response.json();")
            body_used = True
        lines.append(
            "expect(body, "
            f"{js_str('expected body.error.code to be ' + body_key.group(1))}"
            f").toMatchObject({{ error: {{ code: {js_str(body_key.group(1))} }} }});"
        )

    # body.<path> = <value> (string|number|bool)
    body_eq = re.findall(
        r"body\.([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|(true|false|\d+))",
        expected,
    )
    for groups in body_eq:
        if not body_used:
            lines.append("const body = await response.json();")
            body_used = True
        field, dq, sq, lit = groups
        value = dq or sq or lit
        if lit in {"true", "false"} or (lit and lit.isdigit()):
            js_value = lit
        else:
            js_value = js_str(value)
        path_chain = ".".join(field.split("."))
        lines.append(
            f"expect(body.{path_chain}, {js_str('body.' + field + ' must equal ' + value)}).toBe({js_value});"
        )

    # body.<field> must be present / non-empty
    body_present = re.findall(
        r"body\.([A-Za-z_][A-Za-z0-9_.]*)\s+(?:must\s+be\s+)?(?:present|non-empty)",
        expected,
        re.IGNORECASE,
    )
    for field in body_present:
        if not body_used:
            lines.append("const body = await response.json();")
            body_used = True
        lines.append(
            f"expect(body.{field}, {js_str('body.' + field + ' must be present')}).toBeDefined();"
        )

    # header.<Name> = <value>
    header_eq = re.findall(
        r"header[s]?\.([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(?:\"([^\"]+)\"|'([^']+)')",
        expected,
        re.IGNORECASE,
    )
    for name, dq, sq in header_eq:
        value = dq or sq
        lines.append(
            f"expect(response.headers()[{js_str(name.lower())}], "
            f"{js_str('header ' + name + ' must equal ' + value)}).toBe({js_str(value)});"
        )

    # If the plan text mentions body / header but nothing was parsed,
    # refuse to emit a status-only test (issue #95).
    mentions_body = re.search(r"\bbody\b", expected, re.IGNORECASE)
    mentions_header = re.search(r"\bheader\b", expected, re.IGNORECASE)
    if (mentions_body or mentions_header) and not (
        body_used or header_eq
    ):
        raise UsageError(
            f"api generator: expected_assertion for {item.candidate_id} "
            "mentions body/header but the generator cannot convert the "
            "text into an executable assertion. Rewrite as e.g. "
            "'HTTP 200 and body.id present' or 'HTTP 201 and "
            "header.Location = \"/orders/123\"'."
        )

    return "\n    ".join(lines)
