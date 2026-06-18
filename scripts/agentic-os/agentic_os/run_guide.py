"""Human-readable "how to run" guide for a generated suite (issues #371, #372).

The final operator-handoff artifact: a self-contained ``how-to-run.html`` that
ships inside the standalone bundle (#369) so a non-author can run the tests by
following it — prerequisites, exact commands, the two-mode env contract (#370),
what pass/fail means, and where reports / evidence / bug files land.

Self-contained on purpose: inline ``<style>``, no external assets, readable
offline. The markup lives in a template under ``templates/how-to-run.html.template``
(issue #372) and ``render_run_guide_html`` fills it; the CLI ``reports html``
(re)generates it standalone. Values default to the standalone (human-on-host)
mode and can be overridden from config / a run manifest.
"""
from __future__ import annotations

import html as _html
import string
from pathlib import Path
from typing import Any, Dict, Optional

_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "templates" / "how-to-run.html.template"
)

# Defaults describe the standalone (Mode B) human-on-host run. Config / manifest
# values override these (see ``guide_values_from_config``).
_DEFAULTS: Dict[str, str] = {
    "title": "Jak uruchomić wygenerowane testy",
    "runner": "./run-tests.sh",
    "api_base_url": "http://localhost:3000/api",
    "ui_base_url": "http://localhost:3000",
    "credentials_env": "SUT_API_TOKEN",
    "reports_dir": "reports/",
    "evidence_dir": "evidence/",
    "bugs_dir": "bugs/",
    # Issue #373 — direct link to the rendered HTML report in the handoff copy.
    "report_link": "reports/playwright/html/index.html",
}


def render_run_guide_html(*, values: Optional[Dict[str, Any]] = None) -> str:
    """Render the self-contained ``how-to-run.html`` as a string (PL-first).

    Values are HTML-escaped before substitution; unknown placeholders are left
    intact (``safe_substitute``) so a template edit never raises at render time.
    """
    merged = dict(_DEFAULTS)
    if values:
        merged.update({k: v for k, v in values.items() if v is not None})
    escaped = {k: _html.escape(str(v)) for k, v in merged.items()}
    template = string.Template(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    return template.safe_substitute(escaped)


def write_run_guide_html(
    output_dir: Path, *, values: Optional[Dict[str, Any]] = None
) -> Path:
    """Render and write ``how-to-run.html`` into ``output_dir``; return its path.

    Deterministic — re-running with the same values rewrites identical bytes
    (the CLI ``reports html`` relies on this for idempotent regeneration).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "how-to-run.html"
    target.write_text(render_run_guide_html(values=values), encoding="utf-8")
    return target


def guide_values_from_config(repo_root: Path) -> Dict[str, str]:
    """Best-effort guide values from the project config's ``sut`` block.

    Used by the CLI regenerator (#372) so a project's real base URLs / token env
    name appear in the guide. Never raises — a missing or invalid config falls
    back to the standalone defaults.
    """
    try:
        from .config import load_or_default

        sut = (load_or_default(repo_root).raw.get("sut") or {})
    except Exception:
        return {}
    if not isinstance(sut, dict):
        return {}
    values: Dict[str, str] = {}
    api = sut.get("api_base_url") or sut.get("base_url")
    ui = sut.get("ui_url") or sut.get("base_url")
    if api:
        values["api_base_url"] = str(api)
    if ui:
        values["ui_base_url"] = str(ui)
    creds = sut.get("credentials")
    if isinstance(creds, dict) and creds.get("value"):
        values["credentials_env"] = str(creds["value"])
    return values
