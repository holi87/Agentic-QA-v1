"""Compatibility alias for the local dashboard server implementation."""
from __future__ import annotations

import sys

from .routes import dashboard_server as _impl

sys.modules[__name__] = _impl
