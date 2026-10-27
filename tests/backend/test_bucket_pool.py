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
from agent_backend.ingress import Inbound
from container_up.server import ControlService

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
        from unittest.mock import AsyncMock
        self.service.peers[bucket_id] = SimpleNamespace(closed=False, session_call=AsyncMock())

    async def scale(self, bucket_id, replicas):
        if self.fail_stop:
            raise RuntimeError("scale failed")
        self.scaled.append((bucket_id, replicas))
        self.service.peers.pop(bucket_id, None)


@pytest.fixture
def service(tmp_path):
    config = load_frontends(EXAMPLE).frontend("web-test").control_config()
    config.state_root = tmp_path / "control"
    config.runtime_templates["web-test"].workspace_root = tmp_path / "workspaces"
    config.bucket_pool.max_users_per_bucket = 2
    config.frontend_idle_ttl_seconds["web-test"] = 1
    service = ControlService(config)
    service.pool.manager = FakeManager(service)
    yield service
    service.db.close()


async def submit(service, user, request):
    return await service.submit("web-test", Inbound(chat_id="chat", user_id=user, content="hello", request_id=request))


async def complete(service, row):
    for event in service.db.pending(row["bucket_id"]):
        service.db.ack(row["bucket_id"], event.seq, event.event_id)
    await service.on_event(row["bucket_id"], Envelope(type="request.completed", bucket_id=row["bucket_id"],
        request_id=row["request_id"], payload={"request_id": row["request_id"], "status": "completed"}))


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
async def test_completed_users_release_capacity_before_whole_bucket_retires(service):
    first, second = await asyncio.gather(submit(service, "a", "a"), submit(service, "b", "b"))
    await complete(service, first)
    expire(service)
    await service.pool.sweep()
    assert len(service.pool.bindings(first["bucket_id"])) == 1
    assert not service.pool.manager.scaled  # b is still executing; no per-user eviction
    await complete(service, second)
    expire(service)
    await service.pool.sweep()
    assert service.pool.manager.scaled == [(first["bucket_id"], 0)]
    assert service.db.records("binding") == []
    before = workspace_path(service.config.runtime_for("web-test").workspace_root,
                            Identity(frontend_id="web-test", user_id="a"))
    new = await submit(service, "a", "a2")
    assert new["bucket_id"] != first["bucket_id"]
    assert "previous_turns" not in new["submit"]
    assert "session_epoch" not in new["submit"]
    assert service.db.get("binding", Identity(frontend_id="web-test", user_id="a").identity_id)["workspace_path"] == str(before)


@pytest.mark.asyncio
async def test_gateway_handoff_must_finish_before_scale_down(service):
    row = await submit(service, "a", "r")
    await complete(service, row)
    peer = service.peers[row["bucket_id"]]
    peer.session_call.side_effect = RuntimeError("gateway events still pending")
    expire(service)
    await service.pool.sweep()
    assert service.db.get("bucket", row["bucket_id"])["status"] == "draining"
    assert not service.pool.manager.scaled
    peer.session_call.side_effect = None
    await service.pool.sweep()
    assert service.pool.manager.scaled == [(row["bucket_id"], 0)]


@pytest.mark.asyncio
async def test_gateway_backpressure_excludes_bucket_from_new_placement(service):
    first = await submit(service, "a", "r")
    await complete(service, first)
    await service.on_event(first["bucket_id"], Envelope(type="bucket.status", bucket_id=first["bucket_id"],
                                                       payload={"accepting": False}))
    second = await submit(service, "b", "r2")
    assert second["bucket_id"] != first["bucket_id"]


@pytest.mark.asyncio
async def test_idle_bucket_is_not_woken_for_a_returning_user(service):
    buckets = {}
    for user, chat, content in [("alice", "group-1", "Keep this"), ("alice", "group-2", "Other chat"),
                                ("bob", "group-1", "Other user")]:
        row = await service.submit("web-test", Inbound(user_id=user, chat_id=chat, content=content))
        buckets[user] = row["bucket_id"]
        await complete(service, row)
    expire(service)
    await service.pool.sweep()
    restored = await service.submit("web-test", Inbound(user_id="alice", chat_id="group-1", content="Continue"))
    assert restored["bucket_id"] != buckets["alice"]
    assert "previous_turns" not in restored["submit"]


@pytest.mark.asyncio
async def test_failed_creation_releases_user_reservation(service):
    service.pool.manager.fail_start = True
    with pytest.raises(RuntimeError, match="unavailable"):
        await submit(service, "a", "r")
    assert not service.db.records("request")
    assert not service.db.records("binding")
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
    assert not service.pool.bindings(row["bucket_id"])
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
    config = load_frontends(EXAMPLE).frontend("web-test")
    bucket_id = config.control.bucket_pool.name_prefix + "-0123456789ab"
    runtime = config.runtime_config(EXAMPLE, bucket_id)
    assert runtime.state_root == config.runtime.state_root / config.frontend_id / bucket_id
    assert runtime.workspace_root == config.runtime.workspace_root
    assert not hasattr(runtime, "credential_file")
    manifest = BucketManager(config.control_config()).deployment(bucket_id, "web-test")
    pod = manifest["spec"]["template"]["spec"]
    assert len(pod["containers"]) == 1
    assert pod["containers"][0]["args"][-1] == bucket_id
    assert next(v for v in pod["volumes"] if v["name"] == "workspace")["persistentVolumeClaim"]["claimName"] == "opencode-data-pvc"
    assert pod["automountServiceAccountToken"] is False
    assert pod["nodeSelector"] == config.control.bucket_pool.node_selector


def test_original_workspace_layout_is_bucket_independent(tmp_path):
    identity = Identity(frontend_id="web-test", user_id="alice")
    assert workspace_path(tmp_path, identity) == tmp_path / "web-test" / "alice"


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
    assert repeated["result"]["status"] == "completed"
    assert service.pool.manager.created == created


@pytest.mark.asyncio
async def test_reservation_prevents_idle_sweep_and_zero_disables_ttl(service):
    identity = Identity(frontend_id="web-test", user_id="a")
    async with service.pool.assignment(identity):
        expire(service)
        await service.pool.sweep()
        assert not service.pool.manager.scaled
    service.config.frontend_idle_ttl_seconds["web-test"] = 0
    expire(service)
    await service.pool.sweep()
    assert not service.pool.manager.scaled


@pytest.mark.asyncio
async def test_request_during_retirement_uses_another_bucket(service):
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
        waiting = asyncio.create_task(submit(service, "a", "r2"))
        new = await asyncio.wait_for(waiting, 2)
        assert new["bucket_id"] != row["bucket_id"]
    finally:
        stopped.set()
        await task
    new = await asyncio.wait_for(waiting, 2)
    binding = service.db.get("binding", Identity(frontend_id="web-test", user_id="a").identity_id)
    assert binding["bucket_id"] == new["bucket_id"] != row["bucket_id"]


@pytest.mark.asyncio
async def test_returning_user_uses_free_capacity_not_previous_bucket(service):
    service.config.bucket_pool.max_users_per_bucket = 1
    first = await submit(service, "alice", "a1")
    other = await submit(service, "bob", "b1")
    await complete(service, first)
    occupying = await submit(service, "charlie", "c1")
    assert occupying["bucket_id"] == first["bucket_id"]
    await complete(service, other)
    returning = await submit(service, "alice", "a2")
    assert returning["bucket_id"] == other["bucket_id"]


@pytest.mark.asyncio
async def test_kubernetes_api_create_update_scale_and_wait_for_pod_exit():
    config = load_frontends(EXAMPLE).frontend("web-test").control_config()
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
            body = manager.deployment(bucket_id, "web-test")
            body["metadata"]["resourceVersion"] = "1"
            body["status"] = {"replicas": replicas}
        return httpx.Response(status, json=body, request=httpx.Request(method, "https://kubernetes" + path))

    manager.request = request
    await manager.ensure(bucket_id, "web-test")
    assert replicas == 1
    assert any(method == "POST" and path.endswith("/deployments") for method, path, _ in calls)
    await manager.scale(bucket_id, 0)
    assert replicas == 0
    assert calls[-1][1].endswith("/pods")
    assert calls[-1][2]["params"]["labelSelector"].endswith(bucket_id)
