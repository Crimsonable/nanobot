from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_backend.config import load_frontends
from container_up.bucket_manager import BucketManager

EXAMPLE = Path(__file__).resolve().parents[2] / "config/opencode/frontends.json"
BUCKET = "opencode-bucket-0123456789ab"


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr("container_up.bucket_manager.asyncio.sleep", AsyncMock())
    return BucketManager(load_frontends(EXAMPLE).frontend("web-test").control_config())


def deployment(manager, version):
    body = manager.deployment(BUCKET, "web-test")
    body["metadata"]["resourceVersion"] = version
    return body


def scale(version):
    return {"metadata": {"resourceVersion": version}, "spec": {"replicas": 0}}


def script(manager, steps):
    pending = deque(steps)
    writes = []

    async def request(method, path, **kwargs):
        expected_method, suffix, status, body = pending.popleft()
        assert (method, path.endswith(suffix)) == (expected_method, True)
        if method == "PUT":
            writes.append(kwargs["json"])
        return httpx.Response(status, json=body,
                              request=httpx.Request(method, "https://kubernetes" + path))

    manager.request = request
    return pending, writes


@pytest.mark.asyncio
@pytest.mark.parametrize("create_conflict", [False, True])
async def test_cold_start_recovers_deployment_and_scale_conflicts(manager, create_conflict):
    steps = [("GET", BUCKET, 404, {}),
             ("POST", "/deployments", 409 if create_conflict else 201,
              {} if create_conflict else deployment(manager, "1"))]
    if create_conflict:
        steps.append(("GET", BUCKET, 200, deployment(manager, "1")))
    steps += [("PUT", BUCKET, 409, {}),
              ("GET", BUCKET, 200, deployment(manager, "2")),
              ("PUT", BUCKET, 200, {}),
              ("GET", "/scale", 200, scale("3")),
              ("PUT", "/scale", 409, {}),
              ("GET", "/scale", 200, scale("4")),
              ("PUT", "/scale", 200, {})]
    pending, writes = script(manager, steps)
    await manager.ensure(BUCKET, "web-test")
    assert not pending
    assert [w["metadata"]["resourceVersion"] for w in writes] == ["1", "2", "3", "4"]
    assert all(w["spec"]["replicas"] == 1 for w in writes)


@pytest.mark.asyncio
async def test_scale_down_conflict_still_waits_for_pod_exit(manager):
    pending, writes = script(manager, [
        ("GET", "/scale", 200, scale("1")), ("PUT", "/scale", 409, {}),
        ("GET", "/scale", 200, scale("2")), ("PUT", "/scale", 200, {}),
        ("GET", BUCKET, 200, {"status": {"replicas": 0}}),
        ("GET", "/pods", 200, {"items": [{"metadata": {"name": "terminating"}}]}),
        ("GET", BUCKET, 200, {"status": {"replicas": 0}}),
        ("GET", "/pods", 200, {"items": []}),
    ])
    await manager.scale(BUCKET, 0)
    assert not pending
    assert [w["metadata"]["resourceVersion"] for w in writes] == ["1", "2"]
    assert all(w["spec"]["replicas"] == 0 for w in writes)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["ensure", "scale"])
@pytest.mark.parametrize("status", [403, 409, 500])
async def test_write_errors_propagate_with_bounded_conflict_retries(manager, operation, status):
    steps = []
    attempts = 5 if status == 409 else 1
    for i in range(attempts):
        suffix = BUCKET if operation == "ensure" else "/scale"
        body = deployment(manager, str(i)) if operation == "ensure" else scale(str(i))
        steps += [("GET", suffix, 200, body), ("PUT", suffix, status, {})]
    pending, writes = script(manager, steps)
    with pytest.raises(httpx.HTTPStatusError) as error:
        if operation == "ensure":
            await manager.ensure(BUCKET, "web-test")
        else:
            await manager.scale(BUCKET, 1)
    assert error.value.response.status_code == status
    assert not pending
    assert len(writes) == attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_owner", ["bucket", "frontend"])
async def test_retry_rechecks_deployment_ownership(manager, foreign_owner):
    changed = deployment(manager, "2")
    if foreign_owner == "bucket":
        changed["metadata"]["labels"]["bucket-id"] = "another-bucket"
    else:
        changed["metadata"]["annotations"]["opencode.io/frontend-id"] = "another-frontend"
    pending, writes = script(manager, [
        ("GET", BUCKET, 200, deployment(manager, "1")), ("PUT", BUCKET, 409, {}),
        ("GET", BUCKET, 200, changed),
    ])
    with pytest.raises(RuntimeError, match="not owned|another frontend"):
        await manager.ensure(BUCKET, "web-test")
    assert not pending
    assert len(writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("replicas", [0, 1])
async def test_missing_scale_after_conflict(manager, replicas):
    pending, _ = script(manager, [
        ("GET", "/scale", 200, scale("1")), ("PUT", "/scale", 409, {}),
        ("GET", "/scale", 404, {}),
    ])
    if replicas == 0:
        await manager.scale(BUCKET, replicas)
    else:
        with pytest.raises(httpx.HTTPStatusError) as error:
            await manager.scale(BUCKET, replicas)
        assert error.value.response.status_code == 404
    assert not pending
