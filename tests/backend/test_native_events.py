from support import emit_answer
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
    config = RuntimeConfig(frontend_id="web", common_root=tmp_path / "common", bucket_id="b", state_root=tmp_path / "state", workspace_root=tmp_path / "users")
    config.event_forwarding.include_types.extend(["future.event", "future.reasoning", "future.large"])
    config.event_forwarding.include_part_types.append("reasoning")
    runtime = Runtime(config, SimpleNamespace(), SimpleNamespace())
    for user in ["alice", "bob"]:
        submit = Submit(request_id=user, chat_id="chat", identity=Identity(frontend_id="web", user_id=user),
                        content="hello", deadline_at=time.time() + 30)
        runtime.db.put("run", user, {"request_id": user, "status": "running", "session_id": "s-" + user,
            "message_id": "input-" + user, "directory": str(runtime.workspace(submit)),
            "submit": submit.model_dump(mode="json"), "artifacts": []})
        runtime.completions[user] = asyncio.Event()
    yield runtime
    runtime.db.close()


@pytest.mark.asyncio
async def test_sse_parser_handles_fragmented_multiline_frames(callback_runtime):
    from bucket_runtime.opencode_client import OpenCodeClient

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            content = (': keepalive\r\nid: transport-id\r\nevent: message\r\n'
                       'data: {"directory":"/alice",\r\n'
                       'data: "payload":{"id":"evt-1","type":"future.event","properties":{"text":"思考"}}}\r\n\r\n'
                       'data: {"payload":{"type":"incomplete"}}').encode()
            for i in range(0, len(content), 3):
                yield content[i:i + 3]

    def handle(request):
        assert request.url.path == "/global/event"
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(200, headers={"content-type": "text/event-stream; charset=utf-8"}, stream=Stream())

    client = OpenCodeClient(callback_runtime.config.opencode, "password")
    await client.http.aclose()
    async with httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(handle)) as client.http:
        frames = [frame async for frame in client.events()]
    assert frames == [{"directory": "/alice", "payload": {
        "id": "evt-1", "type": "future.event", "properties": {"text": "思考"}}}]


@pytest.mark.asyncio
async def test_global_stream_reconnects_deduplicates_and_routes_users(callback_runtime):
    runtime = callback_runtime
    disconnected, resumed, hold = asyncio.Event(), asyncio.Event(), asyncio.Event()
    subscriptions = []

    async def events():
        subscriptions.append(True)
        yield {"payload": {"type": "server.connected"}}
        yield {"payload": {"type": "server.heartbeat"}}
        yield {"directory": runtime.db.get("run", "alice")["directory"], "payload": {
            "id": "evt-alice", "type": "future.event", "properties": {"sessionID": "s-alice"}}}
        if len(subscriptions) == 1:
            disconnected.set()
            raise httpx.ReadError("connection lost")
        yield {"directory": runtime.db.get("run", "bob")["directory"], "payload": {
            "id": "evt-bob", "type": "future.event", "properties": {"sessionID": "s-bob"}}}
        resumed.set()
        await hold.wait()

    runtime.client.events = events
    task = asyncio.create_task(runtime.native_events.subscribe())
    try:
        await asyncio.wait_for(disconnected.wait(), 2)
        assert not runtime.native_events.connected.is_set()
        await asyncio.wait_for(resumed.wait(), 3)
        assert runtime.native_events.connected.is_set()
        drain(runtime)
        outgoing = runtime.db.pending("gateway")
        own = [e for e in outgoing if e.payload["event"]["type"] == "future.event"]
        assert [(e.event_id, e.payload["user_id"], e.payload["chat_id"]) for e in own] == [
            ("evt-alice", "alice", "chat"), ("evt-bob", "bob", "chat")]
        assert all(e.payload["event"]["type"] != "server.heartbeat" for e in outgoing)
        controls = [e for e in outgoing if e.payload["event"]["type"] == "server.connected"]
        assert len(controls) == 0
        assert all(not e.payload.get("user_id") and e.payload["directory"] == "global" for e in controls)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not runtime.native_events.connected.is_set()


def test_upgrade_preserves_accepted_plugin_queue(callback_runtime):
    from bucket_runtime.native_events import NativeEvents
    runtime = callback_runtime
    raw = raw_event(runtime)
    runtime.db.put("plugin_pending", raw["event_id"], raw)
    runtime.db.accept("plugin", raw["event_id"])
    runtime.native_events = NativeEvents(runtime)
    assert not runtime.db.records("plugin_pending")
    assert runtime.native_event(raw)["duplicate"]
    drain(runtime)
    assert [e.event_id for e in runtime.db.pending("gateway")] == [raw["event_id"]]


@pytest.mark.asyncio
async def test_disconnected_stream_blocks_new_session_until_deadline(callback_runtime):
    runtime = callback_runtime
    row = runtime.db.get("run", "alice")
    row["submit"]["deadline_at"] = time.time() + 0.05
    runtime.db.put("run", "alice", row)
    # The fake client has no session methods: none may be called while SSE is down.
    await runtime.run("alice")
    assert runtime.db.get("run", "alice")["result"]["error"] == "REQUEST_TIMEOUT"


def complete(runtime, user="alice", **updates):
    return emit_answer(runtime, "s-" + user, user, "input-" + user,
        runtime.db.get("run", user)["directory"], {"info": {"id": "answer", "sessionID": "s-" + user,
        "role": "assistant", "parentID": "input-" + user, "time": {"created": 1, "completed": 2},
        "finish": "stop", "tokens": {"input": 12, "output": 4}, **updates},
        "parts": [{"type": "text", "text": "done"}]})


def test_native_completion_is_durable_and_wakes_only_its_request(callback_runtime):
    runtime = callback_runtime
    final = complete(runtime)
    assert runtime.completions["alice"].is_set()
    assert not runtime.completions["bob"].is_set()
    assert runtime.db.get("run", "alice")["result"]["content"] == ""
    assert runtime.db.get("run", "alice")["result"]["usage"] is None
    assert runtime.native_event(final)["duplicate"]
    assert len([e for e in runtime.db.pending("gateway") if e.type == "runtime.result"]) == 1


def test_native_incomplete_message_cannot_finish_request(callback_runtime):
    complete(callback_runtime, finish="tool-calls")
    assert not callback_runtime.completions["alice"].is_set()
    complete(callback_runtime, time={"created": 1})
    assert not callback_runtime.completions["alice"].is_set()


def test_cancelled_request_still_forwards_events_without_finishing(callback_runtime):
    runtime = callback_runtime
    row = runtime.db.get("run", "alice")
    runtime.db.put("run", "alice", {**row, "cancel_requested": True, "native_error": "MessageAbortedError"})
    complete(runtime)
    assert runtime.db.get("run", "alice")["status"] == "cancelled"
    assert runtime.db.get("run", "alice")["result"]["error"] is None



def raw_event(runtime, **updates):
    return {"type": "opencode.event", "event_id": "oc-1", "directory": runtime.db.get("run", "alice")["directory"],
        "event": {"type": "future.reasoning", "properties": {"sessionID": "s-alice", "messageID": "a",
            "partID": "reason", "field": "text", "delta": "思考内容"}}, **updates}


def drain(runtime):
    while runtime.native_events.process_next():
        pass


def send(runtime, event_id, kind, properties):
    runtime.native_event(raw_event(runtime, event_id=event_id, event={"type": kind, "properties": properties}))
    drain(runtime)


@pytest.mark.asyncio
async def test_plugin_handshake_and_removed_event_endpoint(callback_runtime):
    runtime = callback_runtime
    async with httpx.AsyncClient(base_url="http://runtime", transport=httpx.ASGITransport(app=rpc_app(runtime, "private-token"))) as client:
        assert (await client.post("/plugin/event", json=raw_event(runtime))).status_code == 401
        client.headers["authorization"] = "Bearer private-token"
        assert (await client.post("/plugin/ready", json={"version": "4"})).status_code == 409
        assert (await client.post("/plugin/ready", json={"version": "5"})).status_code == 409
        assert (await client.post("/plugin/ready", json={"version": "8"})).status_code == 409
        assert (await client.post("/plugin/ready", json={"version": "9"})).status_code == 200
        assert (await client.post("/authorize", json={})).status_code == 404
        assert (await client.post("/execute", json={"ticket": "obsolete"})).status_code == 404
        assert (await client.post("/plugin/event", json=raw_event(runtime))).status_code == 404


def test_all_raw_events_are_forwarded_without_completing_requests(callback_runtime):
    runtime = callback_runtime
    raw = raw_event(runtime)
    runtime.native_event(raw)
    assert runtime.native_event(raw)["duplicate"]
    drain(runtime)
    forwarded = runtime.db.pending("gateway")[0]
    assert forwarded.event_id == raw["event_id"]
    assert forwarded.payload["event"] == raw["event"]
    assert forwarded.payload["user_id"] == "alice"
    assert forwarded.payload["chat_id"] == "chat"
    assert not runtime.completions["alice"].is_set()
    complete(runtime)
    runtime.native_event(raw_event(runtime, event_id="late"))
    runtime.native_event(raw_event(runtime, event_id="global",
        event={"type": "future.event", "properties": {"sessionID": "s-alice", "text": "x" * 240001}}))
    drain(runtime)
    assert all(e.payload.get("event", {}).get("properties", {}).get("text") != "x" * 240001 for e in runtime.db.pending("gateway"))
    assert runtime.db.get("run", "alice")["result"]["content"] == ""


def test_wrong_workspace_is_retained_without_blocking_other_events(callback_runtime):
    runtime = callback_runtime
    runtime.native_event(raw_event(runtime, directory=runtime.db.get("run", "bob")["directory"]))
    runtime.native_event(raw_event(runtime, event_id="good"))
    drain(runtime)
    assert runtime.db.get("native_failed", "oc-1")
    assert [e.event_id for e in runtime.db.pending("gateway")] == ["good"]


def test_durable_intake_survives_restart_and_outbox_backpressure(callback_runtime):
    from agent_backend.journal import Journal
    runtime = callback_runtime
    runtime.db.max_outbox = 0
    assert runtime.native_event(raw_event(runtime)) == {"ok": True}
    assert not runtime.native_events.process_next()
    assert runtime.db.records("native_pending")
    runtime.db.close()
    runtime.db = Journal(runtime.config.state_root / "runtime.sqlite")
    drain(runtime)
    assert len(runtime.db.pending("gateway")) == 1
    assert not runtime.db.records("native_pending")
    assert runtime.native_event(raw_event(runtime))["duplicate"]


def test_session_error_and_idle_are_correlated_in_runtime(callback_runtime):
    runtime = callback_runtime
    send(runtime, "user", "message.updated", {"info": {"id": "input-alice", "sessionID": "s-alice", "role": "user"}})
    send(runtime, "busy", "session.status", {"sessionID": "s-alice", "status": {"type": "busy"}})
    send(runtime, "error", "session.error", {"sessionID": "s-alice", "error": {"name": "ProviderError"}})
    send(runtime, "done", "session.status", {"sessionID": "s-alice", "status": {"type": "idle"}})
    assert runtime.db.get("run", "alice")["result"]["error"] == "ProviderError"
    assert runtime.db.get("run", "bob")["status"] == "running"
    send(runtime, "idle", "session.idle", {"sessionID": "s-alice"})
    send(runtime, "late", "session.error", {"sessionID": "s-alice", "error": {"name": "Unrelated"}})
    assert len([e for e in runtime.db.pending("gateway") if e.type == "runtime.result"]) == 1


def test_parts_deltas_and_attribution_survive_restart(callback_runtime):
    from agent_backend.journal import Journal
    runtime = callback_runtime
    send(runtime, "info", "message.updated", {"info": {"id": "answer", "sessionID": "s-alice", "role": "assistant", "parentID": "input-alice"}})
    send(runtime, "part", "message.part.updated", {"part": {"id": "p", "sessionID": "s-alice", "messageID": "answer", "type": "text", "text": ""}})
    runtime.db.close()
    runtime.db = Journal(runtime.config.state_root / "runtime.sqlite")
    send(runtime, "delta", "message.part.delta", {"sessionID": "s-alice", "messageID": "answer", "partID": "p", "field": "text", "delta": "durable reply"})
    assert runtime.db.pending("gateway")[-1].request_id == "alice"
    send(runtime, "final", "message.updated", {"info": {"id": "answer", "sessionID": "s-alice", "role": "assistant", "parentID": "input-alice", "finish": "stop", "time": {"created": 1, "completed": 2}}})
    assert runtime.db.get("run", "alice")["status"] == "running"


def test_malformed_event_does_not_block_later_completion(callback_runtime):
    runtime = callback_runtime
    send(runtime, "broken", "message.part.delta", {"sessionID": "s-alice", "messageID": "missing", "partID": "absent", "field": "text", "delta": "oops"})
    assert not runtime.db.get("native_failed", "broken")
    assert not runtime.db.records("message")
    complete(runtime)
    assert runtime.db.get("run", "alice")["status"] == "completed"


def test_child_session_and_compact_incremental_logs(callback_runtime, capsys):
    import json
    runtime = callback_runtime
    context = {"schema_version": 1, "frontend_id": "web", "user_id": "alice", "chat_id": "chat"}
    send(runtime, "root", "session.updated", {"info": {"id": "s-alice", "metadata": {"bucket_runtime": context}}})
    send(runtime, "child", "session.created", {"info": {"id": "child", "parentID": "s-alice"}})
    send(runtime, "part", "message.part.updated", {"part": {"id": "p", "sessionID": "child", "messageID": "answer", "type": "reasoning", "text": "thinking"}})
    send(runtime, "delta", "message.part.delta", {"sessionID": "child", "messageID": "answer", "partID": "p", "field": "text", "delta": " more"})
    send(runtime, "snapshot", "message.part.updated", {"part": {"id": "p", "sessionID": "child", "messageID": "answer", "type": "reasoning", "text": "thinking more"}})
    send(runtime, "tool", "tool.execute.before", {"sessionID": "child", "args": {"command": "echo hi", "api_key": "secret"}})
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert all(set(line) == {"user_id", "frontend_id", "chat_id", "content"} for line in lines)
    assert all(line["user_id"] == "alice" and line["chat_id"] == "chat" for line in lines)
    assert [line["content"] for line in lines] == ["thinking", "thinking more"]
    assert len(lines) == 2  # Only part snapshots, never deltas or tool hooks.
    assert "secret" not in lines[-1]["content"]
    assert not runtime.completions["alice"].is_set()


@pytest.mark.asyncio
async def test_worker_consumes_durable_events(callback_runtime):
    runtime = callback_runtime
    worker = asyncio.create_task(runtime.native_events.run())
    try:
        runtime.native_event(raw_event(runtime))
        for _ in range(20):
            if runtime.db.pending("gateway"):
                break
            await asyncio.sleep(0.01)
        assert len(runtime.db.pending("gateway")) == 1
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


def test_child_without_parent_metadata_uses_runtime_ownership(callback_runtime, capsys):
    import json
    runtime = callback_runtime
    send(runtime, "child", "session.created", {"info": {"id": "child", "parentID": "s-alice"}})
    send(runtime, "text", "message.part.updated", {"part": {"id": "p", "sessionID": "child",
        "messageID": "answer", "type": "text", "text": "child reply"}})
    line = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert line == {"user_id": "alice", "frontend_id": "web", "chat_id": "chat", "content": "child reply"}
    assert not runtime.db.pending("gateway")


def test_runtime_log_settings_do_not_filter_transport(callback_runtime, capsys):
    import json
    runtime = callback_runtime
    runtime.native_events.logger.settings.reasoning = False
    send(runtime, "reasoning", "message.part.updated", {"part": {"id": "p", "sessionID": "s-alice",
        "messageID": "answer", "type": "reasoning", "text": "private reasoning"}})
    assert not capsys.readouterr().out
    assert runtime.db.pending("gateway")[-1].payload["event"]["properties"]["part"]["text"] == "private reasoning"
    runtime.native_events.logger.settings.max_chars = 256
    send(runtime, "text", "message.part.updated", {"part": {"id": "text", "sessionID": "s-alice",
        "messageID": "answer", "type": "text", "text": "x" * 1000}})
    line = json.loads(capsys.readouterr().out)
    assert len(line["content"]) == 256 and line["content"].endswith("…")
    assert len(runtime.db.pending("gateway")[-1].payload["event"]["properties"]["part"]["text"]) == 1000


def test_tool_logs_exclude_rewritten_runtime_arguments(callback_runtime, capsys):
    import json
    runtime = callback_runtime
    send(runtime, "tool-result", "message.part.updated", {"part": {"id": "tool", "sessionID": "s-alice",
        "messageID": "answer", "type": "tool", "state": {"status": "completed",
            "input": {"command": "exec /usr/bin/bwrap -- /bin/sh -c original"}, "output": "generated file"}}})
    line = json.loads(capsys.readouterr().out)
    assert line["content"] == '{"output":"generated file"}'


@pytest.mark.asyncio
async def test_large_events_survive_fragmentation_replay_and_cu_restart(callback_runtime, tmp_path):
    from agent_backend.config import ControlConfig, GatewaySettings
    from agent_backend.journal import Journal
    from agent_backend.ingress import Inbound
    from channel_gateway.server import create_app
    from support import LocalControl

    runtime = callback_runtime
    raw = raw_event(runtime, event={"type": "future.large", "properties": {"sessionID": "s-alice", "text": '工具输出："\\\n' * 100000}})
    runtime.db.max_outbox = 1
    runtime.native_event(raw)
    assert not runtime.native_events.process_next()
    assert not runtime.db.pending("gateway")  # No partial outgoing event was committed.
    assert runtime.db.seen("opencode", raw["event_id"])
    runtime.db.max_outbox = 100
    drain(runtime)
    chunks = runtime.db.pending("gateway")
    assert len(chunks) > 1 and all(e.type == "opencode.event.chunk" for e in chunks)
    assert all(len(e.model_dump_json().encode()) < 1024 * 1024 for e in chunks)
    config = ControlConfig.model_validate({"state_root": str(tmp_path / "cu"),
        "frontends": {"web": {"provider": "web", "channel_config": {}}}})
    local = LocalControl(config)
    settings = GatewaySettings(state_root=config.state_root / "gateway")
    service = create_app(config, settings, control=local).state.service
    try:
        await service.submit("web", Inbound(chat_id="chat", user_id="alice", content="hello", request_id="alice"))
        await service.on_event("b", chunks[-1], "web")
        service.db.close()
        service.db = Journal(settings.state_root / "gateway.sqlite")
        assert not service.db.records("opencode_event")
        for chunk in reversed(chunks):
            await service.on_event("b", chunk, "web")
            await service.on_event("b", chunk, "web")
        saved = service.db.records("opencode_event")
        assert len(saved) == 1
        assert saved[0]["event_id"] == raw["event_id"]
        assert saved[0]["payload"]["event"] == raw["event"]
        assert saved[0]["payload"]["user_id"] == "alice"
        assert not service.db.records("event_chunks")
        assert not service.db.get("request", "alice").get("result")
    finally:
        service.db.close()
        local.service.db.close()


@pytest.mark.asyncio
async def test_cu_uses_received_snapshots_and_delivers_only_final_result(callback_runtime, tmp_path):
    from agent_backend.config import ControlConfig, GatewaySettings
    from agent_backend.ingress import Inbound
    from channel_gateway.server import create_app
    from support import LocalControl
    runtime = callback_runtime
    config = ControlConfig.model_validate({"state_root": str(tmp_path / "cu-final"),
        "frontends": {"web": {"provider": "web", "channel_config": {}}}})
    local = LocalControl(config)
    settings = GatewaySettings(state_root=config.state_root / "gateway")
    service = create_app(config, settings, control=local).state.service
    try:
        await service.submit("web", Inbound(chat_id="chat", user_id="alice", content="hello", request_id="alice"))
        # A delta with no local part is forwarded, never accumulated by runtime.
        send(runtime, "delta-only", "message.part.delta", {"sessionID": "s-alice", "messageID": "answer",
            "partID": "0", "field": "text", "delta": "must not be appended"})
        complete(runtime)
        assert not runtime.db.records("message")
        for event in runtime.db.pending("gateway"):
            await service.on_event("b", event, "web")
            if event.type == "opencode.event":
                assert not service.db.get("request", "alice").get("result")
        result = service.db.get("request", "alice")["result"]
        assert result["content"] == "done"
        assert result["usage"]["output_tokens"] == 4
        assert result["status"] == "completed"
    finally:
        service.db.close()
        local.service.db.close()
