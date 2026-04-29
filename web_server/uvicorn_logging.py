from __future__ import annotations

from copy import deepcopy
import logging

from uvicorn.config import LOGGING_CONFIG


_HEALTH_LOG_PATTERNS = (
    "GET /health/live",
    "GET /health/ready",
    "GET /healthz",
)


class IgnoreHealthcheckAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(pattern in message for pattern in _HEALTH_LOG_PATTERNS)


def build_uvicorn_log_config() -> dict:
    config = deepcopy(LOGGING_CONFIG)
    filters = dict(config.get("filters") or {})
    filters["ignore_healthcheck_access"] = {
        "()": "web_server.uvicorn_logging.IgnoreHealthcheckAccessFilter",
    }
    config["filters"] = filters

    handlers = dict(config.get("handlers") or {})
    access_handler = dict(handlers.get("access") or {})
    access_filters = list(access_handler.get("filters") or [])
    if "ignore_healthcheck_access" not in access_filters:
        access_filters.append("ignore_healthcheck_access")
    access_handler["filters"] = access_filters
    handlers["access"] = access_handler
    config["handlers"] = handlers
    return config
