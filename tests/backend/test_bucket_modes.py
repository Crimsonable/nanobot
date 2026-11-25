import asyncio
import copy
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from support import configure_pool

from agent_backend.config import BucketSettings, ControlConfig, individual_bucket_id
from agent_backend.ingress import Inbound
from agent_backend.journal import Journal
from agent_backend.models import ExecutionStatus, Identity, Submit
from container_up.server import ControlService
from container_up.session_manager import SessionManager


@pytest.fixture
def service(tmp_path):
    config = ControlConfig(state_root=tmp_path / "control",
        frontends={f: {"provider": "web"} for f in ("web", "other")},
        bucket_templates={f: BucketSettings(workspace_root=tmp_path / "users", behavior="individual")
                          for f in ("web", "other")},
        frontend_idle_ttl_seconds={"web": 1, "other": 0},
        bucket_pool={"max_sessions_per_bucket": 1})
    service = ControlService(config)
    configure_pool(service)
    yield service
    service.db.close()


async def send(service, request, *, user="alice", chat="chat", frontend="web"):
    return await service.submit(frontend, Inbound(user_id=user, chat_id=chat,
                                                request_id=request, content="hello"))


async def finish(service, row):
    native = await service.pool.client(row["bucket_id"])
    native.answer(row)
    service.complete(ExecutionStatus(request_id=row["request_id"], status="completed"))
    service.db.put("request", row["request_id"], {**service.db.get("request", row["request_id"]), "output_collected": True})
    service.pool.release(Submit.model_validate(row["submit"]).session_key)


@pytest.mark.asyncio
async def test_individual_concurrent_chats_ignore_capacity_and_separate_identities(service):
    rows = await asyncio.gather(*(send(service, f"r{i}", chat=str(i)) for i in range(5)))
    expected = individual_bucket_id(Identity(frontend_id="web", user_id="alice"))
    assert {r["bucket_id"] for r in rows} == {expected}
    assert len({r["native"]["session_id"] for r in rows}) == 5
    assert service.pool.manager.created == [expected]
    bob, other = await asyncio.gather(send(service, "bob", user="bob"), send(service, "other", frontend="other"))
    assert len({expected, bob["bucket_id"], other["bucket_id"]}) == 3
    assert service.db.get("bucket", expected)["owner"] == {"frontend_id": "web", "user_id": "alice"}


@pytest.mark.asyncio
async def test_switching_modes_migrates_only_requested_chat_and_returns_to_same_user_bucket(service):
    service.config.bucket_templates["web"].behavior = "pool"
    a = await send(service, "a", chat="a")
    b = await send(service, "b", chat="b")
    assert a["bucket_id"] != b["bucket_id"]
    await finish(service, a)
    await finish(service, b)
    service.config.bucket_templates["web"].behavior = "individual"
    a2 = await send(service, "a2", chat="a")
    user_bucket = a2["bucket_id"]
    assert a2["native"]["session_id"] == a["native"]["session_id"]
    assert service.db.get("session", Submit.model_validate(b["submit"]).session_key)["bucket_id"] == b["bucket_id"]
    target = await service.pool.client(user_bucket)
    assert target.history[a2["native"]["session_id"]][0]["id"] == a["native"]["message_id"]
    b2 = await send(service, "b2", chat="b")
    assert b2["bucket_id"] == user_bucket
    await finish(service, a2)
    await finish(service, b2)
    service.config.bucket_templates["web"].behavior = "pool"
    a3 = await send(service, "a3", chat="a")
    assert a3["bucket_id"] != user_bucket
    assert service.db.get("bucket", a3["bucket_id"])["behavior"] == "pool"
    await finish(service, a3)
    service.config.bucket_templates["web"].behavior = "individual"
    a4 = await send(service, "a4", chat="a")
    assert a4["bucket_id"] == user_bucket
    assert a4["native"]["session_id"] == a["native"]["session_id"]
    assert service.db.get("bucket", a["bucket_id"])["behavior"] == "pool"
    assert all("settings" not in bucket and "runtime" not in bucket for bucket in service.db.records("bucket"))
    await finish(service, a4)
    target.export_session = AsyncMock(side_effect=AssertionError("unnecessary transfer"))
    assert (await send(service, "a5", chat="a"))["bucket_id"] == user_bucket


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [False, True])
async def test_switch_does_not_reuse_busy_binding_or_move_uncollected_output(service, completed):
    row = await send(service, "old")
    service.config.bucket_templates["web"].behavior = "pool"
    if completed:
        service.complete(ExecutionStatus(request_id="old", status="completed"))
    with pytest.raises(HTTPException) as error:
        await send(service, "new")
    assert error.value.status_code == 503
    assert service.db.get("request", "new") is None
    assert service.db.get("session", Submit.model_validate(row["submit"]).session_key)["bucket_id"] == row["bucket_id"]
    await finish(service, row)
    assert (await send(service, "new"))["bucket_id"] != row["bucket_id"]


@pytest.mark.asyncio
async def test_switch_does_not_fall_back_to_native_busy_source(service):
    row = await send(service, "old")
    await finish(service, row)
    source = await service.pool.client(row["bucket_id"])
    source.active = True
    service.config.bucket_templates["web"].behavior = "pool"
    with pytest.raises(HTTPException) as error:
        await send(service, "new")
    assert error.value.status_code == 503
    assert not source.deleted
    source.active = False
    assert (await send(service, "new"))["bucket_id"] != row["bucket_id"]


@pytest.mark.asyncio
async def test_resume_recorded_migration_then_route_under_new_mode(service):
    original = await send(service, "old")
    await finish(service, original)
    service.config.bucket_templates["web"].behavior = "pool"
    source = await service.pool.client(original["bucket_id"])
    delete = source.delete_session

    async def lost_reply(sid):
        await delete(sid)
        raise httpx.ReadError("lost delete reply")

    source.delete_session = lost_reply
    with pytest.raises(httpx.ReadError):
        await send(service, "new")
    assert service.db.records("migration")
    source.delete_session = delete
    service.config.bucket_templates["web"].behavior = "individual"
    resumed = await send(service, "new")
    assert resumed["bucket_id"] == original["bucket_id"]
    assert resumed["native"]["session_id"] == original["native"]["session_id"]
    assert not service.db.records("migration")
    assert source.history[resumed["native"]["session_id"]][0]["id"] == original["native"]["message_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_user_bucket_survives_idle_restart_and_missing_deployment(service, missing):
    row = await send(service, "old")
    await finish(service, row)
    bid = row["bucket_id"]
    service.db.put("bucket", bid, {**service.db.get("bucket", bid), "last_active_at": time.time() - 20})
    await service.pool.sweep()
    assert service.pool.manager.scaled == [(bid, 0)]
    service.db.close()
    service.db = Journal(service.config.state_root / "control.sqlite")
    service.pool.db = service.db
    service.sessions = SessionManager(service.config, service.db, service.pool)
    if missing:
        service.pool.manager.missing.add(bid)
    again = await asyncio.wait_for(send(service, "again"), 2)
    assert again["bucket_id"] == bid
    assert again["native"]["session_id"] == row["native"]["session_id"]


@pytest.mark.asyncio
async def test_deterministic_name_cannot_claim_another_owner(service):
    row = await send(service, "old")
    await finish(service, row)
    bucket = copy.deepcopy(service.db.get("bucket", row["bucket_id"]))
    bucket["owner"]["user_id"] = "bob"
    service.db.put("bucket", row["bucket_id"], bucket)
    with pytest.raises(PermissionError, match="owner"):
        await send(service, "new")


@pytest.mark.asyncio
async def test_missing_native_history_is_not_replaced_by_empty_session(service, monkeypatch):
    row = await send(service, "old")
    await finish(service, row)
    client = await service.pool.client(row["bucket_id"])
    response = httpx.Response(404, request=httpx.Request("GET", "http://native/api/session/missing"))
    monkeypatch.setattr(client, "session", AsyncMock(side_effect=httpx.HTTPStatusError(
        "session missing", request=response.request, response=response)))
    with pytest.raises(RuntimeError, match="history is missing"):
        await send(service, "new")
