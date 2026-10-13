import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agent_backend.config import load_frontends
from agent_backend.models import Envelope, Identity
from agent_backend.workspaces import workspace_path
from container_up.bucket_manager import BucketManager
from container_up.server import ControlService, Inbound

EXAMPLE = Path(__file__).resolve().parents[2] / "config/opencode/frontends.json"


class FakeManager:
    def __init__(self, service):
        self.service = service
        self.created = []
        self.scaled = []
        self.fail_start = False
        self.fail_stop = False

    async def ensure(self, bucket_id, frontend_id=None):
        self.created.append(bucket_id)
        await asyncio.sleep(0.01)
        if self.fail_start:
            raise RuntimeError("Kubernetes unavailable")
        self.service.peers[bucket_id] = SimpleNamespace(closed=False)

    async def scale(self, bucket_id, replicas):
        if self.fail_stop:
            raise RuntimeError("scale failed")
        self.scaled.append((bucket_id, replicas))
        self.service.peers.pop(bucket_id, None)


@pytest.fixture
def service(tmp_path):
    config = load_frontends(EXAMPLE).frontend("web-yk").control_config()
    config.state_root = tmp_path / "control"
    config.runtime_template.workspace_root = tmp_path / "workspaces"
    config.bucket_pool.max_users_per_bucket = 2
    config.instance_idle_ttl_seconds = 1
    service = ControlService(config)
    service.pool.manager = FakeManager(service)
    yield service
    service.db.close()


async def submit(service, user, request):
    return await service.submit("web-yk", Inbound(user_id=user, content="hello", request_id=request))


async def complete(service, row):
    for event in service.db.pending(row["bucket_id"]):
        service.db.ack(row["bucket_id"], event.seq, event.event_id)
    await service.on_event(row["bucket_id"], Envelope(type="request.completed", bucket_id=row["bucket_id"],
        request_id=row["request_id"], payload={"request_id": row["request_id"], "status": "completed", "content": "done"}))


def expire(service):
    for bucket in service.db.records("bucket"):
        service.db.put("bucket", bucket["bucket_id"], {**bucket, "last_active_at": time.time() - 10})


@pytest.mark.asyncio
async def test_zero_initial_buckets_capacity_and_concurrent_reservations(service):
    assert service.db.records("bucket") == []
    rows = await asyncio.gather(*(submit(service, f"user-{i}", f"r-{i}") for i in range(5)))
    assert len({r["bucket_id"] for r in rows}) == 3
    assert len(service.pool.manager.created) == 3
    assert all(len(service.pool.bindings(b["bucket_id"])) <= 2 for b in service.db.records("bucket"))
    same = await submit(service, "user-0", "another")
    assert same["bucket_id"] == rows[0]["bucket_id"]
    assert len(service.db.records("binding")) == 5


@pytest.mark.asyncio
async def test_bucket_only_lifecycle_does_not_release_individual_users(service):
    first, second = await asyncio.gather(submit(service, "a", "a"), submit(service, "b", "b"))
    await complete(service, first)
    expire(service)
    await service.pool.sweep()
    assert len(service.pool.bindings(first["bucket_id"])) == 2
    assert not service.pool.manager.scaled  # b is still executing; no per-user eviction
    await complete(service, second)
    expire(service)
    await service.pool.sweep()
    assert service.pool.manager.scaled == [(first["bucket_id"], 0)]
    assert all(b["bucket_id"] is None for b in service.db.records("binding"))
    before = service.db.records("binding")[0]["workspace_path"]
    new = await submit(service, "a", "a2")
    assert new["submit"]["session_epoch"] != first["submit"]["session_epoch"]
    assert new["submit"]["previous_turns"][0]["assistant"] == "done"
    assert service.db.get("binding", Identity(frontend_id="web-yk", user_id="a").key)["workspace_path"] == before


@pytest.mark.asyncio
async def test_failed_creation_rolls_back_user_capacity(service):
    service.pool.manager.fail_start = True
    with pytest.raises(RuntimeError, match="unavailable"):
        await submit(service, "a", "r")
    assert not service.db.records("request")
    assert all(not b["bucket_id"] for b in service.db.records("binding"))
    service.pool.manager.fail_start = False
    await submit(service, "a", "r")
    assert len(service.db.records("bucket")) == 1


@pytest.mark.asyncio
async def test_unacked_commands_and_failed_scale_prevent_release(service):
    row = await submit(service, "a", "r")
    await service.on_event(row["bucket_id"], Envelope(type="request.completed", bucket_id=row["bucket_id"],
        request_id="r", payload={"request_id": "r", "status": "completed"}))
    expire(service)
    await service.pool.sweep()
    assert not service.pool.manager.scaled
    for event in service.db.pending(row["bucket_id"]):
        service.db.ack(row["bucket_id"], event.seq, event.event_id)
    service.pool.manager.fail_stop = True
    with pytest.raises(RuntimeError, match="scale failed"):
        await service.pool.sweep()
    assert service.pool.bindings(row["bucket_id"])
    assert service.db.get("bucket", row["bucket_id"])["status"] == "draining"
    service.pool.manager.fail_stop = False
    await service.pool.sweep()
    assert not service.pool.bindings(row["bucket_id"])


@pytest.mark.asyncio
async def test_restart_keeps_mapping_and_recovers_same_bucket(service):
    first = await submit(service, "a", "r")
    service.peers.clear()
    # Simulate the next CU process opening the persisted database.
    service.db.close()
    restored = ControlService(service.config)
    restored.pool.manager = FakeManager(restored)
    try:
        second = await submit(restored, "a", "r2")
        assert second["bucket_id"] == first["bucket_id"]
        assert restored.pool.manager.created == [first["bucket_id"]]
        assert len(restored.db.pending(first["bucket_id"])) == 2
    finally:
        # Let fixture teardown close the replacement handle once.
        service.db = restored.db


def test_dynamic_config_mounts_and_bucket_auth(tmp_path):
    config = load_frontends(EXAMPLE).frontend("web-yk")
    bucket_id = config.control.bucket_pool.name_prefix + "-0123456789ab"
    runtime = config.runtime_config(EXAMPLE, bucket_id)
    assert runtime.state_root == config.runtime.state_root / config.id / bucket_id
    assert runtime.workspace_root == config.runtime.workspace_root
    assert not hasattr(runtime, "credential_file")
    manifest = BucketManager(config.control_config()).deployment(bucket_id)
    pod = manifest["spec"]["template"]["spec"]
    assert len(pod["containers"]) == 1
    assert pod["containers"][0]["args"][-1] == bucket_id
    assert next(v for v in pod["volumes"] if v["name"] == "workspace")["persistentVolumeClaim"]["claimName"] == "opencode-data-pvc"
    assert pod["automountServiceAccountToken"] is False
    assert pod["nodeSelector"] == config.control.bucket_pool.node_selector


def test_original_workspace_layout_is_bucket_independent(tmp_path):
    identity = Identity(frontend_id="web-yk", user_id="alice")
    assert workspace_path(tmp_path, identity, "frontend_user") == tmp_path / "web-yk" / "alice"


@pytest.mark.asyncio
async def test_sanitized_workspace_collision_rejected(service):
    await submit(service, "a/b", "r1")
    with pytest.raises(ValueError, match="collides"):
        await submit(service, "a-b", "r2")


@pytest.mark.asyncio
async def test_duplicate_after_retirement_does_not_start_bucket(service):
    row = await submit(service, "a", "r")
    await complete(service, row)
    expire(service)
    await service.pool.sweep()
    created = list(service.pool.manager.created)
    repeated = await submit(service, "a", "r")
    assert repeated["result"]["content"] == "done"
    assert service.pool.manager.created == created


@pytest.mark.asyncio
async def test_reservation_prevents_idle_sweep_and_zero_disables_ttl(service):
    identity = Identity(frontend_id="web-yk", user_id="a")
    async with service.pool.assignment(identity):
        expire(service)
        await service.pool.sweep()
        assert not service.pool.manager.scaled
    service.config.instance_idle_ttl_seconds = 0
    expire(service)
    await service.pool.sweep()
    assert not service.pool.manager.scaled


@pytest.mark.asyncio
async def test_slow_retirement_does_not_block_or_erase_new_assignment(service):
    row = await submit(service, "a", "r")
    await complete(service, row)
    expire(service)
    scaling = asyncio.Event()
    stopped = asyncio.Event()
    original = service.pool.manager.scale

    async def slow_scale(bucket_id, replicas):
        scaling.set()
        await stopped.wait()
        await original(bucket_id, replicas)

    service.pool.manager.scale = slow_scale
    task = asyncio.create_task(service.pool.sweep())
    try:
        await asyncio.wait_for(scaling.wait(), 2)
        new = await asyncio.wait_for(submit(service, "a", "r2"), 2)
        assert new["bucket_id"] != row["bucket_id"]
    finally:
        stopped.set()
        await task
    binding = service.db.get("binding", Identity(frontend_id="web-yk", user_id="a").key)
    assert binding["bucket_id"] == new["bucket_id"]


@pytest.mark.asyncio
async def test_kubernetes_api_create_update_scale_and_wait_for_pod_exit():
    config = load_frontends(EXAMPLE).frontend("web-yk").control_config()
    manager = BucketManager(config)
    bucket_id = config.bucket_pool.name_prefix + "-0123456789ab"
    calls = []
    present = False
    replicas = 0

    async def request(method, path, **kwargs):
        nonlocal present, replicas
        calls.append((method, path, kwargs))
        status, body = 200, {}
        if path.endswith("/pods"):
            body = {"items": []}
        elif path.endswith("/scale"):
            if method == "PUT":
                replicas = kwargs["json"]["spec"]["replicas"]
            body = {"metadata": {"resourceVersion": "1"}, "spec": {"replicas": replicas}}
        elif method == "GET" and not present:
            status = 404
        else:
            present = True
            body = manager.deployment(bucket_id)
            body["metadata"]["resourceVersion"] = "1"
            body["status"] = {"replicas": replicas}
        return httpx.Response(status, json=body, request=httpx.Request(method, "https://kubernetes" + path))

    manager.request = request
    await manager.ensure(bucket_id)
    assert replicas == 1
    assert any(method == "POST" and path.endswith("/deployments") for method, path, _ in calls)
    await manager.scale(bucket_id, 0)
    assert replicas == 0
    assert calls[-1][1].endswith("/pods")
    assert calls[-1][2]["params"]["labelSelector"].endswith(bucket_id)
