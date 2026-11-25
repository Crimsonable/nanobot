import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio
from support import LocalControl

from agent_backend.config import ControlConfig, GatewaySettings
from agent_backend.ingress import Inbound
from agent_backend.journal import Journal
from channel_gateway.event_stream import RequestStreams
from channel_gateway.server import create_app


@pytest_asyncio.fixture
async def system(tmp_path):
    config = ControlConfig(state_root=tmp_path / "cu", frontends={"web": {"provider": "web"}})
    control = LocalControl(config)
    channels = SimpleNamespace(deliver=AsyncMock(), parsers={})
    app = create_app(config, GatewaySettings(state_root=tmp_path / "gw"), channels, control)
    native = await control.service.pool.client("b")
    app.state.service.native.watch = Mock()
    app.state.service.native.connection = AsyncMock(
        return_value={"http": native.http, "native": native, "events": []}
    )
    yield app, control.service, native
    app.state.service.db.close()
    await control.close()


async def accept(system, request_id="r", sync=False, stream=False):
    return await system[0].state.service.submit(
        "web",
        Inbound(
            user_id="alice",
            chat_id="chat",
            content="hello",
            request_id=request_id,
            stream=stream,
            metadata={"frontend_id": "forged", "provider": "forged"},
        ),
        sync=sync,
    )


async def answer(system, request_id="r", **kwargs):
    system[2].answer(system[1].db.get("request", request_id), **kwargs)
    await system[0].state.service.native.poll(request_id)


@pytest.mark.asyncio
async def test_direct_native_reply_and_output_ack(system):
    app, cu, _ = system
    gw = app.state.service
    await accept(system)
    await answer(system)
    row = gw.db.get("request", "r")
    assert row["result"]["content"] == "done" and row["result"]["usage"]["output_tokens"] == 4
    assert cu.pool.busy("b") and not cu.db.get("request", "r").get("result")
    assert await gw.native.poll("r")
    assert not cu.pool.busy("b")
    assert cu.db.get("request", "r")["result"] == {
        "request_id": "r",
        "status": "completed",
        "error": None,
    }
    body = gw.response(row)
    assert set(body) == {
        "request_id",
        "status",
        "frontend_id",
        "user_id",
        "chat_id",
        "bucket_id",
        "identity_id",
        "metadata",
        "delivery_state",
        "content",
        "attachments",
        "error",
        "partial",
        "usage",
    }
    assert body["metadata"]["frontend_id"] == "web" and body["metadata"]["provider"] == "web"


@pytest.mark.asyncio
async def test_gateway_keeps_request_pending_while_mode_switch_waits_for_old_output(system):
    from channel_gateway.server import ControlClient

    app, cu, _ = system
    gw = app.state.service
    await accept(system, "old")
    original = gw.control
    remote = ControlClient(GatewaySettings())
    await remote.http.aclose()

    async def response(request):
        from fastapi import HTTPException

        try:
            row = await cu.submit("web", Inbound.model_validate(json.loads(request.content)))
            return httpx.Response(200, json=row)
        except HTTPException as exc:
            return httpx.Response(exc.status_code, json={"detail": exc.detail})

    remote.http = httpx.AsyncClient(base_url="http://control", transport=httpx.MockTransport(response))
    gw.control = remote
    cu.config.bucket_templates["web"].behavior = "individual"
    try:
        row = await accept(system, "new")
        assert row["bucket_id"] == "" and "rejected" not in row
        # Collect through the ordinary gateway path, using the existing local control transport.
        gw.control = original
        await answer(system, "old")
        await gw.native.poll("old")
        gw.control = remote
        await gw.dispatch(gw.db.get("request", "new"))
        row = gw.db.get("request", "new")
        assert row["bucket_id"].startswith("opencode-user-") and "rejected" not in row
        assert cu.db.get("request", "new")["native"]["session_id"] == cu.db.get("request", "old")["native"]["session_id"]
    finally:
        gw.control = original
        await remote.close()


@pytest.mark.asyncio
async def test_snapshot_disconnect_retries_and_ack_failure_never_rebuilds_final(system):
    app, cu, native = system
    gw = app.state.service
    await accept(system)
    native.answer(cu.db.get("request", "r"))
    original = native.http
    async with httpx.AsyncClient(
        base_url="http://native", transport=httpx.MockTransport(lambda _: httpx.Response(503))
    ) as bad:
        gw.native.connection.return_value = {"http": bad, "native": native, "events": []}
        with pytest.raises(httpx.HTTPStatusError):
            await gw.native.poll("r")
    assert not gw.db.get("request", "r").get("result")
    gw.native.connection.return_value = {"http": original, "native": native, "events": []}
    await gw.native.poll("r")
    gw.control.collected = AsyncMock(side_effect=httpx.ConnectError("offline"))
    with pytest.raises(httpx.ConnectError):
        await gw.native.poll("r")
    assert cu.pool.busy("b") and gw.db.get("request", "r")["result"]["content"] == "done"


@pytest.mark.asyncio
async def test_background_jobs_delay_final_response(system):
    app, cu, native = system
    gw = app.state.service
    await accept(system)
    native.jobs = [{"status": "completed"}]
    await answer(system)
    assert not gw.db.get("request", "r").get("result")
    native.jobs = []
    await gw.native.poll("r")
    assert gw.db.get("request", "r")["result"]["status"] == "completed"


@pytest.mark.asyncio
async def test_restart_recovers_snapshot_without_duplicate_usage_or_stream_events(system):
    app, cu, native = system
    gw = app.state.service
    await accept(system, sync=True, stream=True)
    native.active = True
    await answer(system)
    before = gw.streams.last_seq("r")
    gw.db.close()
    gw.db = Journal(gw.settings.state_root / "gateway.sqlite")
    gw.native.db = gw.artifacts.journal = gw.db
    gw.streams = RequestStreams(gw.db)
    native.active = False
    await gw.native.poll("r")
    assert gw.streams.last_seq("r") == before + 1
    assert gw.db.get("request", "r")["result"]["usage"]["output_tokens"] == 4


@pytest.mark.asyncio
async def test_missing_bucket_finishes_without_native_connection(system):
    app, cu, _ = system
    gw = app.state.service
    await accept(system)
    await cu.pool.missing("b")
    gw.native.connection = AsyncMock(
        side_effect=AssertionError("missing bucket must not be queried")
    )
    await gw.native.poll("r")
    assert gw.db.get("request", "r")["result"]["error"] == "BUCKET_MISSING_EXECUTION_INTERRUPTED"


@pytest.mark.asyncio
async def test_uncertain_submission_keeps_id_and_cancel_is_forwarded_after_recovery(system):
    app, cu, native = system
    gw = app.state.service
    original = gw.control.submit

    async def uncertain(frontend, body):
        await original(frontend, body)
        raise httpx.ReadTimeout("lost reply")

    gw.control.submit = uncertain
    row = await accept(system)
    assert not row["bucket_id"] and len(native.inputs) == 1
    gw.db.put("request", "r", {**row, "cancel_requested": True})
    gw.control.submit = original
    await gw.dispatch(gw.db.get("request", "r"))
    assert cu.db.get("request", "r")["result"]["status"] == "cancelled" and len(native.inputs) == 1


@pytest.mark.asyncio
async def test_recovery_does_not_resend_uncertain_channel_delivery(system):
    app, _, _ = system
    gw = app.state.service
    await accept(system)
    await answer(system)
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
async def test_channel_callback_idempotency(system):
    app, cu, native = system
    payload = {
        "event": {
            "user_id": "alice",
            "chat_id": "chat",
            "content": "hi",
            "metadata": {"message_id": "native-1"},
        }
    }
    await app.state.service.dispatch_channel("web", payload)
    await app.state.service.dispatch_channel("web", payload)
    assert len(cu.db.records("request")) == len(native.inputs) == 1
