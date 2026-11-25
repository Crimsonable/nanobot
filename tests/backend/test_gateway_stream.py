import asyncio
import json

import httpx
import pytest
from test_gateway import accept, answer
from test_gateway import system as system


@pytest.mark.asyncio
async def test_sse_replay_preserves_web_contract_and_validates_cursor(system):
    app, _, _ = system
    gw = app.state.service
    row = await accept(system, sync=True, stream=True)
    await answer(system)
    seq = gw.streams.last_seq("r")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gw"
    ) as client:
        response = await client.post("/inbound-sync/web", json=row["input"])
        assert response.status_code == 200 and response.headers["content-type"].startswith(
            "text/event-stream"
        )
        chunks = response.text.split("\n\n")
        assert "event: gateway.start" in chunks[0]
        final = next(c for c in chunks if "event: gateway.result" in c)
        value = json.loads(
            next(line[6:] for line in final.splitlines() if line.startswith("data: "))
        )
        assert value["content"] == "done"
        resumed = await client.post(
            "/inbound-sync/web", json=row["input"], headers={"Last-Event-ID": str(seq)}
        )
        assert resumed.text == ""
        for cursor in ["-1", "abc", str(seq + 1)]:
            assert (
                await client.post(
                    "/inbound-sync/web", json=row["input"], headers={"Last-Event-ID": cursor}
                )
            ).status_code == 400
        assert (
            await client.post("/inbound/web", json={**row["input"], "request_id": "async"})
        ).status_code == 400
        assert (
            await client.post("/inbound-sync/web", json={**row["input"], "content": "changed"})
        ).status_code == 409
        assert (
            await client.post("/inbound-sync/web", json={**row["input"], "stream": "true"})
        ).status_code == 422


@pytest.mark.asyncio
async def test_two_subscribers_disconnect_independently_without_cancel(system):
    app, cu, _ = system
    gw = app.state.service
    await accept(system, sync=True, stream=True)
    first = gw.streams.iterate("r", 1, 2)
    second = gw.streams.iterate("r", 1, 2)
    a = asyncio.create_task(anext(first))
    b = asyncio.create_task(anext(second))
    await asyncio.sleep(0)
    a.cancel()
    await asyncio.gather(a, return_exceptions=True)
    await first.aclose()
    await answer(system)
    assert "opencode.event" in await b
    await second.aclose()
    assert not gw.streams.listeners and not cu.db.get("request", "r").get("cancel_requested")


@pytest.mark.asyncio
async def test_stream_timeout_keeps_request_running_and_json_mode_unchanged(system):
    app, cu, _ = system
    gw = app.state.service
    row = await accept(system, sync=True)
    assert "stream" not in row["input"]
    stream = gw.streams.iterate("r", 0, 0.01, heartbeat=0.001)
    chunks = [chunk async for chunk in stream]
    assert "STREAM_WAIT_TIMEOUT" in chunks[-1] and not cu.db.get("request", "r").get("result")
    await answer(system)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gw"
    ) as client:
        response = await client.post("/inbound-sync/web", json=row["input"])
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["content"] == "done"
    assert gw.streams.last_seq("r") == 0
