import asyncio
import json
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from fastapi import HTTPException
from test_gateway import accept, answer, transfer
from test_gateway import system as system

from agent_backend.journal import Journal
from agent_backend.models import Envelope, Result, Submit, digest
from channel_gateway.event_stream import RequestStreams


def frames(text):
    result = []
    for block in text.split("\n\n"):
        if not block or block.startswith(":"):
            continue
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        fields["data"] = json.loads(fields["data"])
        if "id" in fields:
            fields["id"] = int(fields["id"])
        result.append(fields)
    return result


def event(request_id="r", event_id="native", native=None):
    return Envelope(type="opencode.event", bucket_id="b", request_id=request_id, event_id=event_id,
        payload={"frontend_id": "web", "user_id": "alice", "chat_id": "chat",
                 "session_id": "s", "event": native or {"type": "future.event", "properties": {"x": 1}}})


def result(request_id="r", status="completed"):
    return Envelope(type="runtime.result", bucket_id="b", request_id=request_id,
        payload=Result(request_id=request_id, status=status, content="done").model_dump())


@pytest.mark.asyncio
async def test_http_stream_replays_exact_native_json_and_existing_final_response(system):
    app, cu, bu = system
    gw = app.state.service
    row = await accept(system, sync=True, stream=True)
    assert cu.db.get("request", "r")["submit"]["stream"] is True
    assert bu.db.get("run", "r")["submit"]["stream"] is True
    native = {"id": "original-id", "type": "future.event", "extra": [1, {"hello": "中文\n换行"}]}
    raw = event(native=native)
    await gw.on_event("b", raw, "web")
    await gw.on_event("b", raw, "web")
    answer(bu)
    await transfer(gw, cu, bu)
    terminal = result()
    await gw.on_event("b", terminal, "web")  # A replay cannot change the saved terminal.
    await gw.on_event("b", event(event_id="late"), "web")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post("/inbound-sync/web", json=row["input"])
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        events = frames(response.text)
        assert events[0] == {"id": 1, "event": "gateway.start", "data": {
            "version": 1, "request_id": "r", "frontend_id": "web", "user_id": "alice", "chat_id": "chat"}}
        assert events[1] == {"id": 2, "event": "opencode.event", "data": native}
        assert [e["id"] for e in events] == list(range(1, len(events) + 1))
        assert [e["event"] for e in events].count("gateway.result") == 1
        final = (await client.get("/requests/web/r")).json()
        assert events[-1]["event"] == "gateway.result"
        assert events[-1]["data"] == final
        assert final["content"] == "done" and final["usage"]["output_tokens"] == 4
        replay = await client.post("/inbound-sync/web", json=row["input"], headers={"Last-Event-ID": "2"})
        assert frames(replay.text) == events[2:]
        eof = await client.post("/inbound-sync/web", json=row["input"],
                                headers={"Last-Event-ID": str(events[-1]["id"])})
        assert eof.status_code == 200 and not eof.content
    assert len(cu.db.records("request")) == 1
    delivery = asyncio.create_task(gw.deliveries())
    try:
        await asyncio.sleep(0)
        gw.channels.deliver.assert_not_awaited()
    finally:
        delivery.cancel()
        await asyncio.gather(delivery, return_exceptions=True)


@pytest.mark.asyncio
async def test_stream_policy_forwards_reasoning_unknown_parts_and_children_only_for_owner(system):
    app, _, bu = system
    gw = app.state.service
    await accept(system, sync=True, stream=True)
    run = bu.db.get("run", "r")
    natives = [
        {"type": "message.part.updated", "properties": {"part": {"id": "p", "sessionID": "s",
            "messageID": "answer", "type": "reasoning", "text": "思考"}}},
        {"type": "message.part.delta", "properties": {"sessionID": "s", "messageID": "answer",
            "partID": "p", "field": "text", "delta": "继续"}},
        {"type": "message.part.updated", "properties": {"part": {"id": "t", "sessionID": "s",
            "messageID": "answer", "type": "tool", "callID": "call", "tool": "bash",
            "state": {"status": "completed", "input": {"command": "echo test"}, "output": "test"}}}},
        {"type": "session.created", "properties": {"info": {"id": "child", "parentID": "s"}}},
        {"type": "message.part.updated", "properties": {"part": {"id": "c", "sessionID": "child",
            "messageID": "child-answer", "type": "future-part", "opaque": [1, 2]}}},
        {"type": "future.event", "properties": {"sessionID": "s", "payload": {"unknown": True}}},
        {"type": "message.part.removed", "properties": {"sessionID": "s", "messageID": "answer", "partID": "p"}},
    ]
    for i, native in enumerate(natives + [{"type": "future.event", "properties": {"sessionID": "foreign"}}]):
        bu.native_event({"type": "opencode.event", "event_id": str(i), "directory": run["directory"], "event": native})
        while bu.native_events.process_next():
            pass
    outgoing = bu.db.pending("gateway")
    assert [e.payload["event"] for e in outgoing] == natives
    for envelope in outgoing:
        await gw.on_event("b", envelope, "web")
    assert gw.streams.last_seq("r") == len(natives) + 1
    assert bu.db.get("run", "r")["status"] == "running"


@pytest.mark.asyncio
async def test_stream_restart_chunk_reassembly_and_transactional_dedup(system):
    app, _, bu = system
    gw = app.state.service
    row = await accept(system, sync=True, stream=True)
    raw = event(native={"type": "future.large", "properties": {"text": '工具输出："\\\n' * 100000}})
    bu.gateway_emit(raw)
    chunks = bu.db.pending("gateway")
    assert len(chunks) > 1 and all(e.type == "opencode.event.chunk" for e in chunks)
    await gw.on_event("b", chunks[-1], "web")
    assert gw.streams.last_seq("r") == 1
    gw.db.close()
    gw.db = Journal(gw.settings.state_root / "gateway.sqlite")
    gw.streams = RequestStreams(gw.db)
    for chunk in reversed(chunks):
        await gw.on_event("b", chunk, "web")
        await gw.on_event("b", chunk, "web")
    assert gw.streams.last_seq("r") == 2
    # A failure after intake must roll back both deduplication and stream append.
    with pytest.raises(RuntimeError):
        with gw.db.transaction():
            await gw.on_event("b", event(event_id="rollback"), "web")
            raise RuntimeError("simulated crash")
    assert gw.streams.last_seq("r") == 2
    assert gw.db.get("opencode_event", digest(["b", "rollback"])) is None
    await gw.on_event("b", event(event_id="rollback"), "web")
    await gw.on_event("b", result(), "web")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post("/inbound-sync/web", json=row["input"], headers={"Last-Event-ID": "1"})
    events = frames(response.text)
    assert [e["id"] for e in events] == [2, 3, 4]
    assert events[0]["data"] == raw.payload["event"]
    assert events[-1]["event"] == "gateway.result"


@pytest.mark.asyncio
async def test_independent_live_subscribers_timeout_and_cleanup(system):
    app, _, _ = system
    gw = app.state.service
    await accept(system, sync=True, stream=True)
    a = gw.streams.iterate("r", 0, 10, heartbeat=0.01)
    b = gw.streams.iterate("r", 1, 10, heartbeat=0.01)
    assert frames(await anext(a))[0]["event"] == "gateway.start"
    assert await anext(b) == ": ping\n\n"
    await gw.on_event("b", event(), "web")
    assert frames(await anext(a)) == frames(await anext(b))
    await a.aclose()
    assert len(gw.streams.listeners["r"]) == 1
    await gw.on_event("b", result(status="cancelled"), "web")
    assert frames(await anext(b))[0]["data"]["status"] == "cancelled"
    with pytest.raises(StopAsyncIteration):
        await anext(b)
    assert not gw.streams.listeners
    await accept(system, request_id="timeout", sync=True, stream=True)
    timed = [chunk async for chunk in gw.streams.iterate("timeout", 1, 0.01)]
    assert frames("".join(timed)) == [{"event": "gateway.error", "data": {
        "request_id": "timeout", "code": "STREAM_WAIT_TIMEOUT", "message": "等待超时，请查询请求状态"}}]
    assert not gw.db.get("request", "timeout").get("result")
    assert not gw.streams.listeners


@pytest.mark.asyncio
async def test_resume_validation_and_old_json_contract(system):
    app, _, _ = system
    gw = app.state.service
    row = await accept(system, sync=True, stream=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        for cursor in ["-1", "abc", "", "1.0", "2", "9" * 30]:
            response = await client.post("/inbound-sync/web", json=row["input"], headers={"Last-Event-ID": cursor})
            assert response.status_code == 400
        for body in [{**row["input"], "request_id": "missing"}, {k: v for k, v in row["input"].items() if k != "request_id"}]:
            assert (await client.post("/inbound-sync/web", json=body, headers={"Last-Event-ID": "0"})).status_code == 404
        assert (await client.post("/inbound-sync/web", json={**row["input"], "user_id": "bob"},
                                  headers={"Last-Event-ID": "1"})).status_code == 409
        assert (await client.post("/inbound-sync/web", json={**row["input"], "stream": False})).status_code == 409
        assert (await client.post("/inbound/web", json={**row["input"], "request_id": "async"})).status_code == 400
        assert (await client.post("/inbound-sync/web", json={**row["input"], "stream": "true"})).status_code == 422
        assert gw.db.get("request", "missing") is None
        assert gw.db.get("request", "async") is None
        old = await accept(system, request_id="old", sync=True)
        assert "stream" not in old["input"]
        await gw.on_event("b", result(request_id="old"), "web")
        for extra in [{}, {"stream": False}]:
            response = await client.post("/inbound-sync/web", json={**old["input"], **extra})
            assert response.headers["content-type"] == "application/json"
            assert response.json()["content"] == "done"
        assert gw.streams.last_seq("old") == 0


@pytest.mark.asyncio
async def test_recovered_rejection_wakes_stream_and_foreign_events_cannot_enter(system):
    app, _, _ = system
    gw = app.state.service
    await accept(system, sync=True, stream=True)
    stream = gw.streams.iterate("r", 1, 10)
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    for bad in [event().model_copy(update={"bucket_id": "other"}),
                event().model_copy(update={"payload": {**event().payload, "user_id": "bob"}})]:
        with pytest.raises(PermissionError):
            await gw.on_event(bad.bucket_id, bad, "web")
    assert gw.streams.last_seq("r") == 1
    async def rejected(frontend, body):
        raise HTTPException(409, "rejected after recovery")
    gw.control.submit = rejected
    with pytest.raises(HTTPException):
        await gw.dispatch(gw.db.get("request", "r"))
    frame = frames(await asyncio.wait_for(pending, 1))[0]
    assert frame["data"]["code"] == "REQUEST_REJECTED"
    await stream.aclose()
    assert not gw.streams.listeners


@asynccontextmanager
async def live_gateway(app):
    # Real HTTP is needed: ASGITransport buffers the entire streaming response.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        task.result()
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(task, 5)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def next_frame(lines):
    block = []
    async with asyncio.timeout(3):
        async for line in lines:
            if not line:
                return frames("\n".join(block) + "\n\n")[0]
            block.append(line)
    raise AssertionError("stream ended before the next frame")


@pytest.mark.asyncio
async def test_real_http_delivers_before_completion_and_disconnect_does_not_cancel(system):
    app, cu, _ = system
    gw = app.state.service
    body = {"user_id": "alice", "chat_id": "chat", "request_id": "r", "content": "hi", "stream": True}
    async with live_gateway(app) as url:
        async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
            async with client.stream("POST", "/inbound-sync/web", json=body) as response:
                lines = response.aiter_lines()
                assert (await next_frame(lines))["event"] == "gateway.start"
                assert not gw.db.get("request", "r").get("result")
                assert Submit.model_validate(cu.db.get("request", "r")["submit"]).stream
                await gw.on_event("b", event(), "web")
                assert (await next_frame(lines))["event"] == "opencode.event"
            async with asyncio.timeout(3):
                while gw.streams.listeners:
                    await asyncio.sleep(0.01)
            assert not gw.db.get("request", "r").get("cancel_requested")
            await gw.on_event("b", result(status="failed"), "web")
            resumed = await client.post("/inbound-sync/web", json=body, headers={"Last-Event-ID": "2"})
            assert frames(resumed.text)[0]["data"]["status"] == "failed"
            assert len(cu.db.records("request")) == 1
