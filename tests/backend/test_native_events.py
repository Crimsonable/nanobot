import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from support import emit_answer, native

from agent_backend.config import RuntimeConfig
from agent_backend.models import Identity, Submit
from bucket_runtime.adapter import BucketRuntimeAdapter
from bucket_runtime.opencode_server import OpenCodeServer
from bucket_runtime.server import rpc_app


@pytest.fixture
def callback_runtime(tmp_path):
    config = RuntimeConfig(frontend_id="web", common_root=tmp_path / "common", bucket_id="b",
        state_root=tmp_path / "state", workspace_root=tmp_path / "users")
    config.event_forwarding.include_types.append("future.event")
    runtime = BucketRuntimeAdapter(config, SimpleNamespace(connected=asyncio.Event(), plugin_ready=asyncio.Event(),
        session_tree=AsyncMock(return_value=[{"id": "root"}]), sessions_idle=AsyncMock(return_value=True)), SimpleNamespace())
    for user in ["alice", "bob"]:
        submit = Submit(request_id=user, chat_id="chat", identity=Identity(frontend_id="web", user_id=user),
            content="hello", deadline_at=time.time() + 30)
        runtime.db.put("run", user, {"request_id": user, "status": "running", "session_id": "s-" + user,
            "message_id": "input-" + user, "directory": str(runtime.workspace(submit)),
            "submit": submit.model_dump(mode="json"), "artifacts": []})
        runtime.completions[user] = asyncio.Event()
    yield runtime
    runtime.db.close()


def complete(runtime, user="alice", **kwargs):
    return emit_answer(runtime, "s-" + user, "input-" + user,
        runtime.db.get("run", user)["directory"], **kwargs)


@pytest.mark.asyncio
async def test_sse_parser_handles_fragmented_multiline_frames(callback_runtime):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            content = (': heartbeat\r\nid: transport-id\r\n'
                'data: {"id":"evt-1",\r\n'
                'data: "type":"future.event","data":{"text":"思考"}}\r\n\r\n'
                'data: {"type":"incomplete"}').encode()
            for i in range(0, len(content), 3):
                yield content[i:i + 3]
    def handle(request):
        assert request.url.path == "/api/event"
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(200, headers={"content-type": "text/event-stream; charset=utf-8"}, stream=Stream())
    client = OpenCodeServer(callback_runtime.config, "password")
    await client.http.aclose()
    async with httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(handle)) as client.http:
        frames = [frame async for frame in client.events()]
    assert frames == [{"id": "evt-1", "type": "future.event", "data": {"text": "思考"}}]


@pytest.mark.asyncio
async def test_global_stream_reconnects_and_deduplicates(callback_runtime):
    runtime = callback_runtime
    resumed, hold = asyncio.Event(), asyncio.Event()
    attempts = []
    async def events():
        attempts.append(True)
        yield {"id": "connected", "type": "server.connected", "data": {}}
        yield {"id": "alice-event", "type": "future.event", "data": {"sessionID": "s-alice"}}
        if len(attempts) == 1:
            raise httpx.ReadError("connection lost")
        yield {"id": "bob-event", "type": "future.event", "data": {"sessionID": "s-bob"}}
        resumed.set()
        await hold.wait()
    runtime.opencode = OpenCodeServer(runtime.config)
    runtime.opencode.events = events
    runtime.opencode._on_event = runtime.on_opencode_event
    task = asyncio.create_task(runtime.opencode._subscribe())
    try:
        await asyncio.wait_for(resumed.wait(), 3)
        assert runtime.opencode.connected.is_set()
        while runtime.process_next_event():
            pass
        assert [(e.event_id, e.payload["user_id"]) for e in runtime.db.pending("gateway")] == [
            ("alice-event", "alice"), ("bob-event", "bob")]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.opencode.close()
    assert not runtime.opencode.connected.is_set()


@pytest.mark.asyncio
async def test_disconnected_stream_blocks_prompt_until_deadline(callback_runtime):
    runtime = callback_runtime
    row = runtime.db.get("run", "alice")
    row["submit"]["deadline_at"] = time.time() + 0.02
    runtime.db.put("run", "alice", row)
    await runtime.run("alice")
    assert runtime.db.get("run", "alice")["result"]["error"] == "REQUEST_TIMEOUT"


async def test_native_terminal_is_durable_and_wakes_only_its_request(callback_runtime):
    runtime = callback_runtime
    final = complete(runtime)
    assert runtime.db.get("run", "alice")["status"] == "running"
    await runtime.settle_requests()
    assert runtime.completions["alice"].is_set()
    assert not runtime.completions["bob"].is_set()
    assert runtime.db.get("run", "alice")["result"]["content"] == ""
    assert runtime.native_event(final)["duplicate"]
    assert len([e for e in runtime.db.pending("gateway") if e.type == "runtime.result"]) == 1


async def test_steps_and_child_terminal_do_not_finish_parent(callback_runtime):
    runtime = callback_runtime
    complete(runtime, complete=False)
    assert not runtime.completions["alice"].is_set()
    native(runtime, "child", "session.created", {"parentID": "s-alice",
        "location": {"directory": runtime.db.get("run", "alice")["directory"]}, "metadata": {}})
    native(runtime, "child", "session.execution.succeeded")
    assert not runtime.completions["alice"].is_set()
    native(runtime, "s-alice", "session.execution.succeeded")
    await runtime.settle_requests()
    assert runtime.completions["alice"].is_set()


@pytest.mark.parametrize("kind,data,error", [
    ("session.execution.failed", {"error": {"type": "api", "message": "quota"}}, "quota"),
    ("session.execution.interrupted", {"reason": "shutdown"}, "EXECUTION_INTERRUPTED"),
])
async def test_native_failure_is_correlated(callback_runtime, kind, data, error):
    native(callback_runtime, "s-alice", kind, data)
    await callback_runtime.settle_requests()
    assert callback_runtime.db.get("run", "alice")["result"]["error"] == error
    assert callback_runtime.db.get("run", "bob")["status"] == "running"


@pytest.mark.asyncio
async def test_cancelled_request_waits_for_cleanup_before_finishing(callback_runtime):
    from unittest.mock import AsyncMock

    runtime = callback_runtime
    runtime.opencode.abort = AsyncMock()
    row = runtime.db.get("run", "alice")
    runtime.db.put("run", "alice", {**row, "cancel_requested": True})
    native(runtime, "s-alice", "session.execution.interrupted", {"reason": "user"})
    assert runtime.db.get("run", "alice")["status"] == "running"
    assert not runtime.completions["alice"].is_set()
    await runtime.cancel_request("alice")
    assert runtime.db.get("run", "alice")["result"]["status"] == "cancelled"
    assert runtime.db.get("run", "alice")["result"]["error"] is None


async def test_wrong_workspace_and_malformed_event_do_not_block_others(callback_runtime):
    runtime = callback_runtime
    native(runtime, "s-alice", "future.event", directory=runtime.db.get("run", "bob")["directory"])
    runtime.native_event({"type": "opencode.event", "event_id": "broken", "directory": "global",
                          "event": {"id": "broken", "type": "session.text.ended"}})
    while runtime.process_next_event():
        pass
    assert len(runtime.db.records("native_failed")) == 2
    complete(runtime, "bob")
    await runtime.settle_requests()
    assert runtime.completions["bob"].is_set()


def test_native_intake_survives_restart_and_outbox_backpressure(callback_runtime):
    runtime = callback_runtime
    runtime.db.max_outbox = 1
    native(runtime, "s-alice", "future.event", event_id="first")
    native(runtime, "s-bob", "future.event", event_id="second")
    assert [r["event_id"] for r in runtime.db.records("native_pending")] == ["second"]
    from agent_backend.journal import Journal
    runtime.db.close()
    runtime.db = Journal(runtime.config.state_root / "runtime.sqlite", max_outbox=1)
    first = runtime.db.pending("gateway")[0]
    runtime.db.ack("gateway", first.seq, first.event_id)
    assert runtime.process_next_event()
    assert runtime.db.pending("gateway")[0].event_id == "second"


async def test_delayed_assistant_event_keeps_original_request(callback_runtime):
    runtime = callback_runtime
    complete(runtime)
    await runtime.settle_requests()
    old = runtime.db.get("run", "alice")
    submit = Submit.model_validate(old["submit"]).model_copy(update={"request_id": "next"})
    runtime.db.put("run", "next", {**old, "request_id": "next", "message_id": "input-next", "status": "running",
        "submit": submit.model_dump(mode="json")})
    native(runtime, "s-alice", "session.inbox.delivered", {"inboxID": "input-next"})
    native(runtime, "s-alice", "session.text.ended", {"assistantMessageID": "answer", "ordinal": 0, "text": "late"})
    assert runtime.db.pending("gateway")[-1].request_id == "alice"
    assert runtime.db.get("run", "next")["status"] == "running"


def test_logs_redact_secrets_without_filtering_native_transport(callback_runtime):
    runtime = callback_runtime
    lines = []
    runtime.session_logger.write = lines.append
    native(runtime, "s-alice", "session.tool.called", {"assistantMessageID": "m", "id": "c",
        "input": {"token": "private", "command": "echo hi"}, "executed": True})
    assert "private" not in lines[-1] and "REDACTED" in lines[-1]
    assert runtime.db.pending("gateway")[-1].payload["event"]["data"]["input"]["token"] == "private"


@pytest.mark.asyncio
async def test_plugin_handshake_requires_current_contract(callback_runtime):
    async with httpx.AsyncClient(base_url="http://runtime",
            transport=httpx.ASGITransport(app=rpc_app(callback_runtime, "token"))) as client:
        assert (await client.post("/plugin/ready", json={"version": "10"})).status_code == 401
        client.headers["authorization"] = "Bearer token"
        assert (await client.post("/plugin/ready", json={"version": "9"})).status_code == 409
        assert (await client.post("/plugin/ready", json={"version": "10"})).status_code == 200
        assert (await client.post("/plugin/event", json={})).status_code == 404


@pytest.mark.asyncio
async def test_background_work_defers_root_success_until_native_continuation(callback_runtime):
    runtime = callback_runtime
    async with httpx.AsyncClient(base_url="http://runtime",
            transport=httpx.ASGITransport(app=rpc_app(runtime, "token")),
            headers={"Authorization": "Bearer token"}) as client:
        body = {"session_id": "s-alice", "directory": runtime.db.get("run", "alice")["directory"]}
        assert (await client.post("/session/idle", json={**body, "background": True})).status_code == 200
        native(runtime, "s-alice", "session.execution.succeeded")
        assert runtime.db.get("run", "alice")["status"] == "running"
        assert (await client.post("/session/idle", json={**body, "background": False})).status_code == 200
        native(runtime, "s-alice", "session.execution.succeeded")
        await runtime.settle_requests()
        assert runtime.db.get("run", "alice")["status"] == "completed"


async def test_pending_inbox_keeps_business_request_and_cu_binding(callback_runtime):
    runtime = callback_runtime
    runtime.opencode.sessions_idle.return_value = False
    complete(runtime)
    await runtime.settle_requests()
    assert runtime.db.get("run", "alice")["status"] == "running"
    assert not runtime.completions["alice"].is_set()
    assert not runtime.db.pending("b")
    native(runtime, "s-alice", "session.execution.started")
    runtime.opencode.sessions_idle.return_value = True
    await runtime.settle_requests()
    assert runtime.db.get("run", "alice")["status"] == "running"
    native(runtime, "s-alice", "session.execution.succeeded")
    await runtime.settle_requests()
    assert runtime.db.get("run", "alice")["status"] == "completed"


async def test_cancellation_during_native_idle_read_owns_final_result(callback_runtime):
    runtime = callback_runtime
    complete(runtime)
    async def idle(_):
        row = runtime.db.get("run", "alice")
        runtime.db.put("run", "alice", {**row, "cancel_requested": True})
        return True
    runtime.opencode.sessions_idle.side_effect = idle
    await runtime.settle_requests()
    assert runtime.db.get("run", "alice")["status"] == "running"
    assert not runtime.completions["alice"].is_set()
