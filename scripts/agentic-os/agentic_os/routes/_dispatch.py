"""Small regex route dispatcher for the local dashboard."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, List, Optional, Pattern, Sequence


RouteHandler = Callable[..., Any]


@dataclass(frozen=True)
class MethodRoute:
    method: str
    pattern: Pattern[str]
    handler_name: str

    @classmethod
    def compile(cls, method: str, pattern: str, handler_name: str) -> "MethodRoute":
        return cls(method.upper(), re.compile(pattern), handler_name)


class RouteDispatcher:
    def __init__(self, routes: Sequence[MethodRoute]) -> None:
        self._routes = list(routes)

    @classmethod
    def from_specs(cls, specs: Iterable[tuple[str, str, str]]) -> "RouteDispatcher":
        return cls([MethodRoute.compile(method, pattern, handler) for method, pattern, handler in specs])

    def allowed_methods(self, path: str) -> List[str]:
        methods = {
            route.method
            for route in self._routes
            if route.pattern.fullmatch(path)
        }
        return sorted(methods)

    def dispatch(self, target: Any, method: str, path: str) -> bool:
        method = method.upper()
        for route in self._routes:
            match = route.pattern.fullmatch(path)
            if route.method != method or match is None:
                continue
            handler = getattr(target, route.handler_name)
            handler(*match.groups())
            return True
        return False
