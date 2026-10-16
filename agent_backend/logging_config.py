"""Shared access logging for CU and bucket runtime."""

import logging
from copy import deepcopy

from uvicorn.config import LOGGING_CONFIG


class HealthAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn access arguments: client, method, request target, version, status.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            path = str(record.args[2]).split("?", 1)[0]
            return path not in {"/health/live", "/health/ready"}
        return True


def access_log_config() -> dict:
    config = deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["health_access"] = {"()": HealthAccessFilter}
    config["handlers"]["access"]["filters"] = ["health_access"]
    return config
