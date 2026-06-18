"""Mixin package extracted from routes/dashboard_server.py (issue #292)."""
from .static import StaticMixin
from .config import ConfigMixin
from .work_items import WorkItemsMixin
from .autonomy import AutonomyMixin
from .metrics import MetricsMixin
from .sessions import SessionsMixin
from .inbox import InboxMixin
from .skills import SkillsMixin
from .suggestions import SuggestionsMixin
from .events import EventsMixin
from .decisions import DecisionsMixin

__all__ = [
    "StaticMixin",
    "ConfigMixin",
    "WorkItemsMixin",
    "AutonomyMixin",
    "MetricsMixin",
    "SessionsMixin",
    "InboxMixin",
    "SkillsMixin",
    "SuggestionsMixin",
    "EventsMixin",
    "DecisionsMixin",
]
