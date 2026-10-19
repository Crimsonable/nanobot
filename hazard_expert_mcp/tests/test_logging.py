import logging

import pytest

from hazard_expert_mcp.app import HealthAccessFilter, logging_config


@pytest.mark.parametrize("path,status,visible", [
    ("/healthz", 200, False),
    ("/healthz/?probe=1", 200, False),
    ("/healthz", 503, True),
    ("/healthz", 404, True),
    ("/mcp", 200, True),
    ("/mcp?healthz=1", 200, True),
])
def test_health_access_filter(path, status, visible):
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                               '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1", "GET", path, "1.1", status), None)
    assert HealthAccessFilter().filter(record) is visible


def test_errors_and_unstructured_logs_are_retained():
    record = logging.LogRecord("uvicorn.error", logging.ERROR, "", 0, "healthz failed", (), None)
    assert HealthAccessFilter().filter(record)
    assert logging_config()["handlers"]["access"]["filters"] == ["health_access"]
