import asyncio
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from support import configure_pool, register_bucket

from agent_backend.config import BucketSettings, ControlConfig
from agent_backend.ingress import Inbound
from agent_backend.journal import Journal
from agent_backend.models import ExecutionStatus, Submit
from container_up.server import ControlService
from container_up.session_manager import SessionManager


@pytest.fixture
def service(tmp_path):
    config = ControlConfig(
        state_root=tmp_path / "cu",
        frontends={f: {"provider": "web"} for f in ["web", "other"]},
        bucket_templates={
            f: BucketSettings(workspace_root=tmp_path / "users") for f in ["web", "other"]
        },
        frontend_idle_ttl_seconds={"web": 1, "other": 0},
        bucket_pool={"max_sessions_per_bucket": 2},
    )
    value = ControlService(config)
    configure_pool(value)
    yield value
    value.db.close()


async def submit(service, user, request, chat="chat", frontend="web"):
    return await service.submit(
        frontend, Inbound(user_id=user, chat_id=chat, content="hello", request_id=request)
    )


def collected(service, row):
    service.complete(ExecutionStatus(request_id=row["request_id"], status="completed"))
    row = service.db.get("request", row["request_id"])
    service.db.put("request", row["request_id"], {**row, "output_collected": True})
    service.pool.release(Submit.model_validate(row["submit"]).session_key)


def expire(service):
    for bucket in service.db.records("bucket"):
        service.db.put(
            "bucket", bucket["bucket_id"], {**bucket, "last_active_at": time.time() - 10}
        )


@pytest.fixture
async def previous_session(service):
    register_bucket(service, "a")
    row = await submit(service, "alice", "previous")
    native = await service.pool.client("a")
    native.answer(row)
    collected(service, row)
    assert not service.pool.bindings("a")
    return row, native


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_original_bucket_precedes_less_loaded_bucket(service, previous_session, restart):
    previous, source = previous_session
    await submit(service, "bob", "busy")
    register_bucket(service, "b")
    if restart:
        service.db.close()
        service.db = Journal(service.config.state_root / "control.sqlite")
        service.pool.db = service.db
        service.sessions = SessionManager(service.config, service.db, service.pool)
    source.export_session = AsyncMock(side_effect=AssertionError("unnecessary migration"))
    again = await submit(service, "alice", "again")
    assert again["bucket_id"] == "a"
    assert again["native"]["session_id"] == previous["native"]["session_id"]
    assert len(service.pool.bindings("a")) == 2
    assert not service.pool.bindings("b")
    assert not source.deleted
    assert not service.db.records("migration")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["creating", "idle", "draining"])
async def test_original_bucket_can_finish_startup_or_shutdown_and_resume(
    service, previous_session, status
):
    previous, source = previous_session
    register_bucket(service, "b")
    service.db.put("bucket", "a", {**service.db.get("bucket", "a"), "status": status})
    source.export_session = AsyncMock(side_effect=AssertionError("unnecessary migration"))
    entered = asyncio.Event()
    original_ready = service.pool.ready

    async def ready(bucket_id, **kwargs):
        entered.set()
        return await original_ready(bucket_id, **kwargs)

    service.pool.ready = ready
    task = asyncio.create_task(submit(service, "alice", "again"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        key = Submit.model_validate(previous["submit"]).session_key
        assert service.db.get("binding", key)["bucket_id"] == "a"
        if status == "draining":
            assert not task.done()
            service.db.put("bucket", "a", {**service.db.get("bucket", "a"), "status": "idle"})
            service.pool.stopped["a"].set()
        again = await asyncio.wait_for(task, 2)
        assert again["bucket_id"] == "a"
        assert again["native"]["session_id"] == previous["native"]["session_id"]
        assert service.db.get("bucket", "a")["status"] == "running"
        assert service.pool.manager.created == ["a"]
        assert not source.deleted
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [False, True])
async def test_existing_binding_stays_in_full_bucket_until_output_collected(service, completed):
    register_bucket(service, "a")
    previous = await submit(service, "alice", "previous")
    await submit(service, "bob", "busy")
    register_bucket(service, "b")
    if completed:
        service.complete(ExecutionStatus(request_id="previous", status="completed"))
    again = await submit(service, "alice", "again")
    assert again["bucket_id"] == "a"
    assert again["native"]["session_id"] == previous["native"]["session_id"]
    assert len(service.pool.bindings("a")) == 2
    assert not service.pool.bindings("b")
    collected(service, previous)
    assert len(service.pool.bindings("a")) == 2
    collected(service, again)
    assert len(service.pool.bindings("a")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["user", "chat", "frontend"])
async def test_original_bucket_affinity_uses_full_business_session_key(
    service, previous_session, field
):
    previous, source = previous_session
    await submit(service, "bob", "busy")
    register_bucket(service, "b", frontend_id="other" if field == "frontend" else "web")
    values = {"user": "alice", "chat": "chat", "frontend": "web"}
    values[field] = "other"
    new = await submit(service, request="new", **values)
    assert new["bucket_id"] == "b"
    assert new["native"]["session_id"] != previous["native"]["session_id"]
    assert previous["native"]["session_id"] in source.sessions
    assert not source.deleted


@pytest.mark.asyncio
@pytest.mark.parametrize("already_missing", [False, True])
async def test_missing_original_bucket_does_not_loop_on_affinity(
    service, previous_session, already_missing
):
    previous, source = previous_session
    register_bucket(service, "b")
    service.pool.manager.missing.add("a")
    if already_missing:
        service.db.put(
            "bucket", "a", {**service.db.get("bucket", "a"), "status": "missing", "opencode": None}
        )
    again = await asyncio.wait_for(submit(service, "alice", "again"), 2)
    assert again["bucket_id"] == "b"
    assert again["native"]["session_id"] == previous["native"]["session_id"]
    assert source.deleted == [previous["native"]["session_id"]]
    assert not service.db.records("migration")


@pytest.mark.asyncio
async def test_concurrent_returns_cannot_both_claim_last_original_slot(service, previous_session):
    previous, source = previous_session
    other = await submit(service, "bob", "other")
    source.answer(other)
    collected(service, other)
    service.config.bucket_pool.max_sessions_per_bucket = 1
    register_bucket(service, "b")
    rows = await asyncio.gather(
        submit(service, "alice", "alice-next"), submit(service, "bob", "bob-next")
    )
    assert {row["bucket_id"] for row in rows} == {"a", "b"}
    assert len(service.pool.bindings("a")) == len(service.pool.bindings("b")) == 1
    for before, after in zip([previous, other], rows, strict=True):
        assert before["native"]["session_id"] == after["native"]["session_id"]
        native = await service.pool.client(after["bucket_id"])
        history = native.history[after["native"]["session_id"]]
        assert history[0]["id"] == before["native"]["message_id"]
        assert history[-1]["id"] == after["native"]["message_id"]


@pytest.mark.asyncio
async def test_concurrent_capacity_and_same_user_independent_chats(service):
    rows = await asyncio.gather(
        *(submit(service, "alice", f"r{i}", chat=f"chat{i}") for i in range(5))
    )
    assert len({r["bucket_id"] for r in rows}) == 3
    assert len({r["native"]["session_id"] for r in rows}) == 5
    assert len({r["native"]["directory"] for r in rows}) == 1
    again = await submit(service, "alice", "again", chat="chat0")
    assert again["native"]["session_id"] == rows[0]["native"]["session_id"]
    assert len(service.pool.manager.created) == 3


@pytest.mark.asyncio
async def test_collected_output_releases_capacity_and_native_idle_controls_retirement(service):
    row = await submit(service, "alice", "r")
    expire(service)
    service.complete(ExecutionStatus(request_id="r", status="completed"))
    await service.pool.sweep()
    assert not service.pool.manager.scaled
    collected(service, row)
    expire(service)
    native = await service.pool.client(row["bucket_id"])
    native.jobs = [{"id": "background"}]
    await service.pool.sweep()
    assert not service.pool.manager.scaled
    native.jobs = []
    await service.pool.sweep()
    assert service.pool.manager.scaled == [(row["bucket_id"], 0)]
    assert (await submit(service, "alice", "r"))["result"]["status"] == "completed"
    assert len(service.pool.manager.created) == 1


@pytest.mark.asyncio
async def test_frontend_pool_ownership_and_zero_ttl(service):
    first = await submit(service, "alice", "first")
    second = await submit(service, "alice", "second", frontend="other")
    assert first["bucket_id"] != second["bucket_id"]
    for row in [first, second]:
        collected(service, row)
    expire(service)
    await service.pool.sweep()
    assert service.pool.manager.scaled == [(first["bucket_id"], 0)]


@pytest.mark.asyncio
async def test_workspace_sanitizer_collision_is_rejected(service):
    await submit(service, "a/b", "first")
    with pytest.raises(ValueError, match="collides"):
        await submit(service, "a-b", "second")


@pytest.mark.asyncio
async def test_failed_creation_releases_reservation(service):
    service.pool.manager.fail_start = True
    with pytest.raises(RuntimeError, match="Kubernetes"):
        await submit(service, "alice", "r")
    assert not service.db.records("binding")
    assert not any(service.pool.pins.values())


@pytest.mark.asyncio
async def test_missing_bucket_waits_for_old_writer_before_terminal(service):
    row = await submit(service, "alice", "r")
    entered = asyncio.Event()
    leave = asyncio.Event()

    async def stopped(bucket):
        entered.set()
        await leave.wait()

    service.pool.manager.wait_stopped = stopped
    task = asyncio.create_task(service.pool.missing(row["bucket_id"]))
    await entered.wait()
    assert not service.db.get("request", "r").get("result")
    leave.set()
    await task
    assert (
        service.db.get("request", "r")["result"]["error"] == "BUCKET_MISSING_EXECUTION_INTERRUPTED"
    )


@pytest.mark.asyncio
async def test_slow_creation_does_not_block_another_chat(service):
    manager = service.pool.manager
    original = manager.ensure
    entered = asyncio.Event()
    leave = asyncio.Event()

    async def ensure(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            await leave.wait()
        await original(*args, **kwargs)

    manager.ensure = ensure
    service.config.bucket_pool.max_sessions_per_bucket = 1
    first = asyncio.create_task(submit(service, "alice", "one", chat="slow"))
    await entered.wait()
    second = await asyncio.wait_for(submit(service, "alice", "two", chat="fast"), 2)
    leave.set()
    one = await first
    assert one["bucket_id"] != second["bucket_id"]


@pytest.mark.asyncio
async def test_kubernetes_error_does_not_interrupt_execution(service):
    row = await submit(service, "alice", "r")
    service.pool.manager.exists = AsyncMock(side_effect=httpx.ConnectError("api down"))
    with pytest.raises(httpx.ConnectError):
        await service.pool.ready(row["bucket_id"])
    assert not service.db.get("request", "r").get("result")
    assert service.db.get("bucket", row["bucket_id"])["status"] == "running"
