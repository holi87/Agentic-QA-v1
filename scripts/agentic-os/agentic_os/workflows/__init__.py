"""Compatibility facade for workflow entrypoints.

Workflow policy is split behind this package while preserving the historic
`agentic_os.workflows` import surface.
"""
from __future__ import annotations

from . import _legacy as _impl

globals().update(
    {
        name: getattr(_impl, name)
        for name in dir(_impl)
        if not name.startswith("__")
    }
)

__all__ = [
    name
    for name in globals()
    if not name.startswith("__") and name not in {"_impl"}
]
