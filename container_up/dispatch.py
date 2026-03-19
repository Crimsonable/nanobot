from __future__ import annotations

from collections.abc import Callable
from typing import Any

EventHandler = Callable[[dict[str, Any]], Any]


class DispatchParser:
    def __init__(self, *, event_key: str = "event_type") -> None:
        self.event_key = event_key
        self._handlers: dict[str, EventHandler] = {}

    def register(self, event_type: str) -> Callable[[EventHandler], EventHandler]:
        def decorator(handler: EventHandler) -> EventHandler:
            self._handlers[event_type] = handler
            return handler

        return decorator

    def parse(self, event: dict[str, Any]) -> Any:
        event_type = str(event[self.event_key])
        handler = self._handlers[event_type]
        return handler(event)


dispatch_parser = DispatchParser()


@dispatch_parser.register("check_url")
def parse_check_url(_: dict[str, Any]) -> str:
    return "success"
