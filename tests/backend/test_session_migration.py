import copy
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from support import register_bucket

from agent_backend.config import ControlConfig
from agent_backend.ingress import Inbound
from agent_backend.journal import Journal
from agent_backend.models import ExecutionStatus, Submit
from agent_backend.session_history import SessionBusyError
from container_up.server import ControlService
from container_up.session_manager import SessionManager


@pytest_asyncio.fixture
async def migration(tmp_path):
    service = ControlService(
        ControlConfig(state_root=tmp_path / "cu", frontends={"web": {"provider": "web"}})
    )
    register_bucket(service, "a")
    register_bucket(service, "b")
    row = await service.submit(
        "web", Inbound(user_id="alice", chat_id="chat", request_id="old", content="old")
    )
    assert row["bucket_id"] == "a"
    source = await service.pool.client("a")
    target = await service.pool.client("b")
    source.answer(row)
    sid = row["native"]["session_id"]
    source.sessions["child"] = {
        **copy.deepcopy(source.sessions[sid]),
        "id": "child",
        "parentID": sid,
    }
    source.history["child"] = [
        {"id": "child-user", "type": "user", "text": "child", "time": {"created": 1}}
    ]
    service.complete(ExecutionStatus(request_id="old", status="completed"))
    service.db.put("request", "old", {**service.db.get("request", "old"), "output_collected": True})
    submit = Submit.model_validate(row["submit"])
    service.pool.release(submit.session_key)
    yield service, submit, source, target
    await source.close()
    await target.close()
    service.db.close()


async def move(migration):
    service, submit, _, _ = migration
    return await service.sessions.prepare(
        submit, "b", Path(service.db.get("request", "old")["native"]["directory"])
    )


async def fill_original_bucket(service):
    service.config.bucket_pool.max_sessions_per_bucket = 1
    spare = service.db.get("bucket", "b")
    service.db.put("bucket", "b", {**spare, "status": "idle"})
    blocker = await service.submit("web", Inbound(user_id="bob", request_id="busy", content="busy"))
    service.db.put("bucket", "b", spare)
    assert blocker["bucket_id"] == "a"
    return blocker


@pytest.mark.asyncio
async def test_native_tree_transfer_preserves_ids_and_verifies_before_deletion(migration):
    service, submit, source, target = migration
    original = copy.deepcopy(source.history)
    sid = await move(migration)
    assert target.history == original and not source.history
    assert source.deleted == ["child", sid]
    assert service.db.get("session", submit.session_key)["bucket_id"] == "b"
    assert not service.db.records("migration")
    assert not list((service.config.state_root / "session-transfers").glob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("spare_bucket", [True, False])
async def test_full_original_bucket_routes_and_imports_history_before_new_input(
    migration, spare_bucket
):
    service, business, source, _ = migration
    original = copy.deepcopy(source.history)
    sid = business.session_id
    await fill_original_bucket(service)
    if not spare_bucket:
        blocker = await service.submit(
            "web", Inbound(user_id="charlie", request_id="busy-b", content="busy")
        )
        assert blocker["bucket_id"] == "b"

    row = await service.submit(
        "web", Inbound(user_id="alice", chat_id="chat", request_id="next", content="next")
    )
    if spare_bucket:
        assert row["bucket_id"] == "b"
    else:
        assert row["bucket_id"] not in {"a", "b"}
    target = await service.pool.client(row["bucket_id"])
    assert row["native"]["session_id"] == sid
    assert target.history[sid][:-1] == original[sid]
    assert target.history[sid][-1]["id"] == row["native"]["message_id"]
    assert target.history["child"] == original["child"]
    assert target.sessions["child"]["parentID"] == sid
    assert source.deleted == ["child", sid]
    assert service.db.get("session", business.session_key)["bucket_id"] == row["bucket_id"]
    assert not service.db.records("migration")
    assert len(service.pool.bindings("a")) == 1
    assert len(service.pool.bindings(row["bucket_id"])) == 1


@pytest.mark.asyncio
async def test_interrupted_transfer_keeps_target_when_original_bucket_frees_up(migration):
    service, business, source, target = migration
    blocker = await fill_original_bucket(service)
    original = target.import_session
    target.import_session = AsyncMock(side_effect=httpx.ConnectError("target offline"))
    inbound = Inbound(user_id="alice", chat_id="chat", request_id="next", content="next")
    with pytest.raises(httpx.ConnectError):
        await service.submit("web", inbound)
    assert service.db.get("migration", business.session_key)["target"] == "b"
    service.complete(ExecutionStatus(request_id="busy", status="completed"))
    service.db.put(
        "request", "busy", {**service.db.get("request", "busy"), "output_collected": True}
    )
    service.pool.release(Submit.model_validate(blocker["submit"]).session_key)
    assert not service.pool.bindings("a")
    target.import_session = original
    row = await service.submit("web", inbound)
    assert row["bucket_id"] == "b"
    assert row["native"]["session_id"] == business.session_id
    assert source.deleted == ["child", business.session_id]
    assert not service.db.records("migration")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["import_before", "import_after", "delete_before", "delete_after"]
)
async def test_lost_http_reply_restarts_transfer_without_losing_history(migration, failure):
    service, submit, source, target = migration
    object_ = target if failure.startswith("import") else source
    method = "import_session" if failure.startswith("import") else "delete_session"
    original = getattr(object_, method)

    async def fail(*args):
        if failure.endswith("after"):
            await original(*args)
        raise httpx.ReadError("lost reply")

    setattr(object_, method, fail)
    with pytest.raises(httpx.ReadError):
        await move(migration)
    assert service.db.get("session", submit.session_key)["bucket_id"] == "a"
    assert service.db.records("migration") and service.pool.busy("a") and service.pool.busy("b")
    setattr(object_, method, original)
    service.db.close()
    service.db = Journal(service.config.state_root / "control.sqlite")
    service.pool.db = service.db
    service.sessions = SessionManager(service.config, service.db, service.pool)
    await move(migration)
    assert not source.sessions and "child" in target.sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["history", "owner", "workspace"])
async def test_import_conflicts_never_delete_source(migration, kind):
    _, submit, source, target = migration
    sid = next(iter(source.sessions))
    target.sessions[sid] = copy.deepcopy(source.sessions[sid])
    target.history[sid] = copy.deepcopy(source.history[sid])
    if kind == "history":
        target.history[sid][0]["text"] = "different"
    if kind == "owner":
        target.sessions[sid]["metadata"]["managed"]["user_id"] = "other"
    if kind == "workspace":
        target.sessions[sid]["location"]["directory"] += "-other"
    with pytest.raises(RuntimeError, match="readback mismatch"):
        await move(migration)
    assert not source.deleted


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", ["active", "jobs"])
async def test_pending_native_work_prevents_migration(migration, pending):
    service, _, source, _ = migration
    if pending == "active":
        source.active = True
    else:
        source.jobs = [{"id": "done-but-unconsumed"}]
    with pytest.raises(SessionBusyError):
        await move(migration)
    assert not service.db.records("migration") and not source.deleted


@pytest.mark.asyncio
async def test_changed_source_after_failed_import_does_not_get_deleted(migration):
    _, _, source, target = migration
    original = target.import_session
    target.import_session = AsyncMock(side_effect=httpx.ConnectError("offline"))
    with pytest.raises(httpx.ConnectError):
        await move(migration)
    target.import_session = original
    source.history[next(iter(source.history))].append(
        {"id": "late", "type": "user", "text": "changed"}
    )
    with pytest.raises(RuntimeError, match="readback mismatch"):
        await move(migration)
    assert not source.deleted
