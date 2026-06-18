"""Prompt assembly helpers."""
from __future__ import annotations


def wrap_untrusted(label: str, text: str, max_chars: int = 4000) -> str:
    """Wrap SUT or third-party text so models treat it as data only."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    safe_label = str(label).replace("\n", " ").replace("\r", " ")
    safe_text = str(text)[:max_chars]
    fence = "````" if "```" in safe_text else "```"
    return (
        f"<untrusted-input source={safe_label!r}>\n"
        f"{fence}\n{safe_text}\n{fence}\n"
        f"</untrusted-input>"
    )
