"""Escaping helpers for generated JavaScript/TypeScript sources."""
from __future__ import annotations

import json
import re

from ..errors import UsageError


_UI_ASSERTION_TOKEN_RE = re.compile(r"^[A-Za-z0-9/_:.\-?=&#]{1,200}$")


def js_str(value: str) -> str:
    """Return a JSON-quoted JS string literal."""
    return json.dumps(str(value))


def js_comment_text(value: str) -> str:
    """Return text safe to embed inside a JS line or block comment."""
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text.replace("*/", "* /")


def validate_ui_assertion_token(value: str) -> str:
    """Validate plan-captured URL/regex tokens before RegExp emission."""
    if not _UI_ASSERTION_TOKEN_RE.fullmatch(value):
        raise UsageError(
            "ui generator: URL assertion token contains unsupported characters "
            f"or is too long: {value!r}"
        )
    return value
