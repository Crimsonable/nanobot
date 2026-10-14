import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from agent_backend.config import RuntimeConfig
from agent_backend.models import Identity, Submit
from bucket_runtime.runtime import Runtime
from bucket_runtime.server import rpc_app


@pytest.fixture
def callback_runtime(tmp_path):
    config = RuntimeConfig(bucket_id="b", state_root=tmp_path / "state", workspace_root=tmp_path / "users",
                           profiles={"p": {"model": "test/model"}})
    runtime = Runtime(config, SimpleNamespace(), SimpleNamespace())
    for user in ["alice", "bob"]:
        submit = Submit(request_id=user, chat_id="chat", identity=Identity(frontend_id="web", user_id=user),
                        profile_id="p", content="hello", deadline_at=time.time() + 30)
        runtime.db.put("run", user, {"request_id": user, "status": "running", "session_id": "s-" + user,
            "message_id": "input-" + user, "directory": str(runtime.workspace(submit)),
            "submit": submit.model_dump(mode="json"), "artifacts": []})
        runtime.completions[user] = asyncio.Event()
    yield runtime
    runtime.db.close()


def event(runtime, user="alice", **updates):
    return {"type": "response.delta", "session_id": "s-" + user, "request_id": user,
            "input_message_id": "input-" + user, "directory": runtime.db.get("run", user)["directory"],
            "revision": 1, "content": "partial", **updates}


def answer(user="alice", **updates):
    return {"info": {"id": "answer", "sessionID": "s-" + user, "role": "assistant",
        "parentID": "input-" + user, "time": {"created": 1, "completed": 2}, "finish": "stop",
        "tokens": {"input": 12, "output": 4}, **updates}, "parts": [{"type": "text", "text": "done"}]}


def test_plugin_completion_is_durable_idempotent_and_wakes_only_its_request(callback_runtime):
    runtime = callback_runtime
    assert runtime.plugin_event(event(runtime)) == {"ok": True}
    assert runtime.plugin_event(event(runtime))["duplicate"]
    final = event(runtime, type="result", revision=2, messages=[answer()])
    runtime.plugin_event(final)
    assert runtime.completions["alice"].is_set()
    assert not runtime.completions["bob"].is_set()
    row = runtime.db.get("run", "alice")
    assert row["result"]["content"] == "done"
    assert row["result"]["usage"]["output_tokens"] == 4
    assert runtime.plugin_event(final)["ignored"]
    assert runtime.plugin_event(event(runtime, type="error", revision=3, error="late error"))["ignored"]
    assert runtime.db.get("run", "alice")["result"] == row["result"]
    pending = runtime.db.pending("b")
    assert [e.type for e in pending] == ["response.delta", "request.completed"]


def test_plugin_rejects_wrong_user_directory_message_and_premature_final(callback_runtime):
    runtime = callback_runtime
    for changes in [{"session_id": "s-bob"}, {"input_message_id": "input-bob"},
                    {"directory": runtime.db.get("run", "bob")["directory"]}]:
        with pytest.raises(PermissionError):
            runtime.plugin_event(event(runtime, **changes))
    with pytest.raises(PermissionError):
        runtime.plugin_event(event(runtime, type="result", messages=[answer("bob")]))
    for message in [answer(finish="tool-calls"), answer(time={"created": 1})]:
        with pytest.raises(ValueError, match="not a completed"):
            runtime.plugin_event(event(runtime, type="result", messages=[message]))
    assert not runtime.completions["alice"].is_set()
    runtime.plugin_event(event(runtime, type="error", error="APIError"))
    assert runtime.db.get("run", "alice")["result"]["error"] == "APIError"


def test_plugin_reply_after_cancellation_is_ignored(callback_runtime):
    runtime = callback_runtime
    row = runtime.db.get("run", "alice")
    runtime.db.put("run", "alice", {**row, "cancel_requested": True})
    assert runtime.plugin_event(event(runtime, type="result", messages=[answer()]))["ignored"]
    assert not runtime.completions["alice"].is_set()
    assert not runtime.db.pending("b")


@pytest.mark.asyncio
async def test_plugin_callback_authentication_and_protocol(callback_runtime):
    app = rpc_app(callback_runtime, "private-token")
    async with httpx.AsyncClient(base_url="http://runtime", transport=httpx.ASGITransport(app=app)) as client:
        assert (await client.post("/plugin/event", json=event(callback_runtime))).status_code == 401
        client.headers["authorization"] = "Bearer private-token"
        assert (await client.post("/plugin/ready", json={"version": "1"})).status_code == 409
        assert (await client.post("/plugin/ready", json={"version": "2"})).status_code == 409
        assert (await client.post("/plugin/ready", json={"version": "3"})).status_code == 200
        assert (await client.post("/plugin/event", json=event(callback_runtime))).status_code == 200
        assert (await client.post("/plugin/event", json=event(callback_runtime, session_id="wrong"))).status_code == 403


def raw_event(runtime, **updates):
    return {"type": "opencode.event", "event_id": "oc-1", "directory": runtime.db.get("run", "alice")["directory"],
        "request_id": "alice", "session_id": "s-alice", "input_message_id": "input-alice", "message_id": "a",
        "event": {"type": "message.part.delta", "properties": {"sessionID": "s-alice", "messageID": "a",
            "partID": "reason", "field": "text", "delta": "思考内容"}}, **updates}


def test_all_raw_events_are_forwarded_without_completing_requests(callback_runtime):
    runtime = callback_runtime
    raw = raw_event(runtime)
    runtime.plugin_event(raw)
    assert runtime.plugin_event(raw)["duplicate"]
    forwarded = runtime.db.pending("b")[0]
    assert forwarded.event_id == raw["event_id"]
    assert forwarded.payload["event"] == raw["event"]
    assert forwarded.payload["user_id"] == "alice"
    assert forwarded.payload["chat_id"] == "chat"
    assert not runtime.completions["alice"].is_set()
    runtime.plugin_event(event(runtime, type="result", messages=[answer()]))
    # Terminal and cancellation filters apply to replies, never to raw events.
    runtime.plugin_event(raw_event(runtime, event_id="late"))
    runtime.plugin_event(raw_event(runtime, event_id="global", request_id="", session_id="", input_message_id="",
        event={"type": "future.event", "properties": {"text": "x" * 240001}}))
    assert [e.type for e in runtime.db.pending("b")] == [
        "opencode.event", "request.completed", "opencode.event", "opencode.event"]
    assert len(runtime.db.pending("b")[-1].payload["event"]["properties"]["text"]) == 240001
    assert runtime.db.get("run", "alice")["result"]["content"] == "done"


def test_raw_event_ownership_and_atomic_outbox(callback_runtime):
    runtime = callback_runtime
    for changes in [{"request_id": "bob"}, {"session_id": "s-bob"}, {"input_message_id": "input-bob"},
                    {"directory": runtime.db.get("run", "bob")["directory"]}]:
        with pytest.raises(PermissionError):
            runtime.plugin_event(raw_event(runtime, **changes))
    runtime.db.max_outbox = 0
    with pytest.raises(RuntimeError, match="OUTBOX_FULL"):
        runtime.plugin_event(raw_event(runtime))
    assert not runtime.db.seen("plugin", "oc-1")
    runtime.db.max_outbox = 100
    assert runtime.plugin_event(raw_event(runtime)) == {"ok": True}


@pytest.mark.asyncio
async def test_large_events_survive_fragmentation_replay_and_cu_restart(callback_runtime, tmp_path):
    from agent_backend.config import ControlConfig
    from agent_backend.journal import Journal
    from container_up.server import Inbound, create_app

    runtime = callback_runtime
    raw = raw_event(runtime, event={"type": "future.large", "properties": {"text": '工具输出："\\\n' * 100000}})
    runtime.db.max_outbox = 1
    with pytest.raises(RuntimeError, match="OUTBOX_FULL"):
        runtime.plugin_event(raw)
    assert not runtime.db.pending("b")  # No partial event or dedup marker was committed.
    assert not runtime.db.seen("plugin", raw["event_id"])
    runtime.db.max_outbox = 100
    runtime.plugin_event(raw)
    chunks = runtime.db.pending("b")
    assert len(chunks) > 1 and all(e.type == "opencode.event.chunk" for e in chunks)
    assert all(len(e.model_dump_json().encode()) < 1024 * 1024 for e in chunks)
    config = ControlConfig.model_validate({"state_root": str(tmp_path / "cu"), "bucket_ids": ["b"],
        "frontends": {"web": {"provider": "web", "profile_id": "p", "channel_config": {}}}})
    service = create_app(config).state.service
    service.peers["b"] = SimpleNamespace(closed=False)
    try:
        await service.submit("web", Inbound(user_id="alice", content="hello", request_id="alice"))
        await service.on_event("b", chunks[-1])
        service.db.close()
        service.db = Journal(config.state_root / "control.sqlite")
        assert not service.db.records("opencode_event")
        for chunk in reversed(chunks):
            await service.on_event("b", chunk)
            await service.on_event("b", chunk)
        saved = service.db.records("opencode_event")
        assert len(saved) == 1
        assert saved[0]["event_id"] == raw["event_id"]
        assert saved[0]["payload"]["event"] == raw["event"]
        assert saved[0]["payload"]["user_id"] == "alice"
        assert not service.db.records("opencode_event_chunks")
        assert not service.db.get("request", "alice").get("result")
    finally:
        service.db.close()
