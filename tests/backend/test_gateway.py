import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_backend.config import ControlConfig, EventForwarding, GatewaySettings, RuntimeConfig
from agent_backend.ingress import Inbound
from agent_backend.models import Envelope, Result, Submit, digest
from agent_backend.wire import Peer
from bucket_runtime.runtime import Runtime
from channel_gateway.server import create_app
from support import LocalControl, emit_answer


@pytest.fixture
def system(tmp_path):
    config = ControlConfig(state_root=tmp_path / "cu", frontends={"web": {"provider": "web"}})
    control = LocalControl(config)
    channels = SimpleNamespace(deliver=AsyncMock())
    app = create_app(config, GatewaySettings(state_root=tmp_path / "gw"), channels, control)
    common = tmp_path / "common"
    (common / "web/app").mkdir(parents=True)
    runtime = Runtime(RuntimeConfig(frontend_id="web", bucket_id="b", common_root=common,
        state_root=tmp_path / "bu", workspace_root=tmp_path / "users"), SimpleNamespace(), SimpleNamespace())
    yield app, control.service, runtime
    app.state.service.db.close()
    control.service.db.close()
    runtime.db.close()


async def accept(system, request_id="r", sync=False, stream=False):
    app, cu, bu = system
    gw = app.state.service
    row = await gw.submit("web", Inbound(user_id="alice", chat_id="chat", content="hello", request_id=request_id,
        metadata={"reply_target": {"to_group_id": "group"}, "frontend_id": "forged"}, stream=stream), sync=sync)
    submit = Submit.model_validate(cu.db.get("request", request_id)["submit"])
    bu.db.put("run", request_id, {"request_id": request_id, "status": "running", "session_id": "s",
        "message_id": "input", "directory": str(bu.workspace(submit)), "submit": submit.model_dump(mode="json"),
        "artifacts": [], "event_forwarding": bu.config.event_forwarding.model_dump()})
    return row


def answer(bu):
    row = bu.db.get("run", "r")
    emit_answer(bu, "s", "r", "input", row["directory"], {"info": {
        "id": "answer", "sessionID": "s", "parentID": "input", "role": "assistant",
        "time": {"created": 1, "completed": 2}, "finish": "stop", "tokens": {"input": 12, "output": 4}},
        "parts": [{"type": "text", "text": "done"}]})


async def transfer(gw, cu, bu):
    for event in bu.db.pending("b"):
        await cu.on_event("b", event)
        bu.db.ack("b", event.seq, event.event_id)
    for event in bu.db.pending("gateway"):
        await gw.on_event("b", event, "web")
        bu.db.ack("gateway", event.seq, event.event_id)


@pytest.mark.asyncio
async def test_final_external_fields_unchanged_and_cu_has_no_content(system):
    app, cu, bu = system
    gw = app.state.service
    await accept(system)
    answer(bu)
    await transfer(gw, cu, bu)
    assert not cu.db.records("opencode_event")
    assert cu.db.get("request", "r")["result"] == {"request_id": "r", "status": "completed", "error": None}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        body = (await client.get("/requests/web/r")).json()
    assert set(body) == {"request_id", "status", "frontend_id", "user_id", "chat_id", "bucket_id", "identity_id",
                         "metadata", "delivery_state", "content", "attachments", "error", "partial", "usage"}
    assert body["content"] == "done" and body["usage"]["output_tokens"] == 4
    from channel_gateway.channel_service import ChannelService
    channels = ChannelService(gw.config)
    send = AsyncMock()
    channels.parsers["web"] = SimpleNamespace(post_message=send)
    await channels.deliver(gw.db.get("request", "r"), Result.model_validate(gw.db.get("request", "r")["result"]))
    assert send.call_args.kwargs["payload"] == {
        "chat_id": "chat", "content": "done", "attachments": [], "metadata": {
            "reply_target": {"to_group_id": "group"}, "frontend_id": "web", "user_id": "alice", "request_id": "r",
            "usage": {"input_tokens": 12, "output_tokens": 4, "reasoning_tokens": 0,
                      "cache_read_tokens": 0, "cache_write_tokens": 0}}}


@pytest.mark.asyncio
async def test_idle_is_required_and_drain_waits_for_gateway_ack(system):
    app, cu, bu = system
    await accept(system)
    row = bu.db.get("run", "r")
    def event(kind, props):
        bu.native_event({"type": "opencode.event", "event_id": str(time.time_ns()), "directory": row["directory"],
                         "event": {"type": kind, "properties": props}})
        while bu.native_events.process_next():
            pass
    event("session.status", {"sessionID": "s", "status": {"type": "idle"}})
    assert bu.db.get("run", "r")["status"] == "running"  # Unrelated pre-submit idle.
    event("message.updated", {"info": {"id": "answer", "sessionID": "s", "parentID": "input",
        "role": "assistant", "time": {"completed": 2}, "finish": "stop"}})
    assert bu.db.get("run", "r")["status"] == "running"
    answer(bu)
    with pytest.raises(RuntimeError, match="events still pending"):
        await bu.session_operation({"operation": "drain", "session_id": "b"})
    await transfer(app.state.service, cu, bu)
    await bu.session_operation({"operation": "drain", "session_id": "b"})
    assert bu.intake_closed


@pytest.mark.asyncio
async def test_filter_applies_to_deltas_and_child_idle_does_not_end_parent(system):
    _, _, bu = system
    await accept(system)
    row = bu.db.get("run", "r")
    def send(event):
        bu.native_event({"type": "opencode.event", "event_id": str(time.time_ns()), "directory": row["directory"], "event": event})
        while bu.native_events.process_next():
            pass
    send({"type": "message.part.updated", "properties": {"part": {"id": "thought", "sessionID": "s",
        "messageID": "answer", "type": "reasoning", "text": "private"}}})
    send({"type": "message.part.delta", "properties": {"sessionID": "s", "messageID": "answer",
        "partID": "thought", "field": "text", "delta": " secret"}})
    send({"type": "session.created", "properties": {"info": {"id": "child", "parentID": "s"}}})
    send({"type": "session.status", "properties": {"sessionID": "child", "status": {"type": "idle"}}})
    assert not bu.db.pending("gateway")
    assert bu.db.get("run", "r")["status"] == "running"
    assert not bu.db.pending("b")
    with pytest.raises(ValueError):
        EventForwarding(include_types=["message.part.delta"])


class Socket:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.other = None
        self.drop_ack = False

    async def send(self, packet):
        if not (self.drop_ack and json.loads(packet)["type"] == "ack"):
            await self.other.queue.put(packet)

    async def recv(self):
        return await self.queue.get()


@pytest.mark.asyncio
async def test_independent_gateway_ws_replay_does_not_repeat_delivery(system):
    app, cu, bu = system
    gw = app.state.service
    await accept(system)
    answer(bu)
    async def reject(event):
        raise AssertionError("gateway must not issue commands")
    async def receive(event):
        await gw.on_event("b", event, "web")
    async def roundtrip(drop):
        a, b = Socket(), Socket()
        a.other, b.other, b.drop_ack = b, a, drop
        peers = [Peer(a, bu.db, "b", reject, journal_peer="gateway", protocol="agent-gateway/1"),
                 Peer(b, gw.db, "b", receive, protocol="agent-gateway/1")]
        tasks = [asyncio.create_task(p.run()) for p in peers]
        try:
            async with asyncio.timeout(3):
                while not gw.db.get("request", "r").get("result") or (not drop and bu.db.pending("gateway")):
                    for task in tasks:
                        if task.done():
                            task.result()
                    await asyncio.sleep(0.01)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    await roundtrip(True)
    assert bu.db.pending("gateway") and bu.db.pending("b")
    records = len(gw.db.records("opencode_event"))
    await roundtrip(False)
    assert not bu.db.pending("gateway") and bu.db.pending("b")
    assert len(gw.db.records("opencode_event")) == records
    delivery = asyncio.create_task(gw.deliveries())
    try:
        async with asyncio.timeout(3):
            while gw.db.get("request", "r")["delivery"] != "sent":
                await asyncio.sleep(0.01)
        assert gw.channels.deliver.await_count == 1
    finally:
        delivery.cancel()
        await asyncio.gather(delivery, return_exceptions=True)


@pytest.mark.asyncio
async def test_gateway_rejects_foreign_bucket_and_frontend(system):
    app, _, _ = system
    await accept(system)
    event = Envelope(type="runtime.result", bucket_id="other", request_id="r",
                     payload={"request_id": "r", "status": "completed"})
    with pytest.raises(PermissionError):
        await app.state.service.on_event("other", event, "web")
    with pytest.raises(PermissionError):
        await app.state.service.on_event("other", event, "foreign")


@pytest.mark.asyncio
async def test_recovery_keeps_uncertain_delivery_unknown(system):
    app, _, bu = system
    await accept(system)
    answer(bu)
    await transfer(app.state.service, system[1], bu)
    gw = app.state.service
    row = gw.db.get("request", "r")
    gw.db.put("request", "r", {**row, "delivery": "sending"})
    task = asyncio.create_task(gw.deliveries())
    try:
        await asyncio.sleep(0)
        assert gw.db.get("request", "r")["delivery"] == "unknown"
        gw.channels.deliver.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_migration_preserves_delivery_and_idempotency(system, tmp_path):
    from channel_gateway.migrate import migrate
    from agent_backend.journal import Journal
    app, cu, _ = system
    body = Inbound(user_id="alice", chat_id="chat", content="legacy", request_id="old")
    row = await cu.submit("web", body)
    with pytest.raises(ValueError, match="finish or cancel"):
        migrate(cu.config.state_root / "control.sqlite", tmp_path / "import.sqlite")
    cu.db.put("request", "old", {**row, "status": "completed", "delivery": "sending",
        "metadata": body.metadata, "sync": False,
        "hash": digest({"identity": {"frontend_id": "web", "user_id": "alice"},
                        "input": body.model_dump(exclude={"request_id"}), "sync": False}),
        "result": Result(request_id="old", status="completed", content="legacy result").model_dump()})
    for event in cu.db.pending("b"):
        cu.db.ack("b", event.seq, event.event_id)
    gw = app.state.service
    target = gw.settings.state_root / "gateway.sqlite"
    gw.db.close()
    try:
        assert migrate(cu.config.state_root / "control.sqlite", target) == 1
        assert migrate(cu.config.state_root / "control.sqlite", target) == 0
    finally:
        gw.db = Journal(target)
    restored = await gw.submit("web", body)
    assert restored["delivery"] == "unknown"
    assert restored["result"]["content"] == "legacy result"


@pytest.mark.asyncio
async def test_runtime_control_remains_writable_when_gateway_outbox_full(system):
    _, _, bu = system
    await accept(system)
    bu.db.max_outbox = 1
    bu.gateway_emit(Envelope(type="opencode.event", bucket_id="b", request_id="r", payload={}))
    assert not bu.accepting()
    bu.finish(Result(request_id="r", status="failed", error="REQUEST_TIMEOUT"))
    assert bu.db.pending("b")[0].payload["error"] == "REQUEST_TIMEOUT"
    assert [e.type for e in bu.db.pending("gateway")] == ["opencode.event", "runtime.result"]


@pytest.mark.asyncio
async def test_uncertain_submission_keeps_id_and_recovers_without_duplicate(system):
    app, cu, _ = system
    gw = app.state.service
    original = gw.control.submit
    calls = []
    async def uncertain(frontend, body):
        calls.append(body["request_id"])
        await original(frontend, body)
        raise httpx.ReadTimeout("lost accepted response")
    gw.control.submit = uncertain
    row = await gw.submit("web", Inbound(user_id="alice", chat_id="chat", content="hello", request_id="r"))
    assert row["request_id"] == "r" and not row["bucket_id"]
    assert len(cu.db.records("request")) == 1
    gw.control.submit = original
    await gw.dispatch(row)
    assert gw.db.get("request", "r")["bucket_id"] == "b"
    assert len(cu.db.pending("b")) == 1
    assert calls == ["r"]


@pytest.mark.asyncio
async def test_channel_callback_message_id_is_idempotent(system):
    app, cu, _ = system
    payload = {"event": {"user_id": "alice", "chat_id": "chat", "content": "hello",
                         "metadata": {"message_id": "native-1", "reply_target": {"to_group_id": "group"}}}}
    await app.state.service.dispatch_channel("web", payload)
    await app.state.service.dispatch_channel("web", payload)
    assert len(cu.db.records("request")) == 1
    assert len(cu.db.pending("b")) == 1


@pytest.mark.asyncio
async def test_cancel_during_uncertain_submission_is_forwarded_after_recovery(system):
    app, _, _ = system
    gw = app.state.service
    original = gw.control.submit
    gw.control.submit = AsyncMock(side_effect=httpx.ConnectError("offline"))
    gw.control.cancel = AsyncMock()
    await gw.submit("web", Inbound(user_id="alice", chat_id="chat", content="hello", request_id="r"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        assert (await client.post("/requests/web/r/cancel")).status_code == 200
    assert gw.db.get("request", "r")["cancel_requested"]
    gw.control.cancel.assert_not_awaited()
    gw.control.submit = original
    await gw.dispatch(gw.db.get("request", "r"))
    gw.control.cancel.assert_awaited_once_with("web", "r")
    assert gw.db.get("request", "r")["cancel_forwarded"]
