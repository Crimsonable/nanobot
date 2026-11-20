"""Native interruption acknowledgement must not be mistaken for settled execution."""
import asyncio

import httpx
import pytest

from agent_backend.config import RuntimeConfig
from bucket_runtime.opencode_server import OpenCodeServer


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [True, False])
async def test_abort_waits_even_when_execution_was_already_stopping(interrupted):
    client = OpenCodeServer(RuntimeConfig(frontend_id="web", bucket_id="b"))
    await client.http.aclose()
    waiting, settled = asyncio.Event(), asyncio.Event()
    calls = []

    async def handle(request):
        calls.append(request.url.path)
        assert request.method == "POST" and not request.url.params
        if request.url.path == "/api/session/s/interrupt":
            return httpx.Response(200, json={"interrupted": interrupted})
        assert request.url.path == "/api/experimental/session/s/wait"
        waiting.set()
        await settled.wait()
        return httpx.Response(204)

    async with httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(handle)) as client.http:
        stopping = asyncio.create_task(client.abort("s"))
        try:
            await asyncio.wait_for(waiting.wait(), 2)
            assert not stopping.done()
            settled.set()
            await asyncio.wait_for(stopping, 2)
            assert calls == ["/api/session/s/interrupt", "/api/experimental/session/s/wait"]
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["interrupt", "wait", "timeout"])
async def test_abort_propagates_unconfirmed_cleanup(failure):
    client = OpenCodeServer(RuntimeConfig(frontend_id="web", bucket_id="b"))
    await client.http.aclose()
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/interrupt"):
            return httpx.Response(503 if failure == "interrupt" else 200, json={"interrupted": True})
        assert request.url.path == "/api/experimental/session/s/wait"
        if failure == "timeout":
            raise httpx.ReadTimeout("cleanup still running", request=request)
        return httpx.Response(503)

    async with httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(handle)) as client.http:
        with pytest.raises(httpx.ReadTimeout if failure == "timeout" else httpx.HTTPStatusError):
            await client.abort("s")
    assert len(calls) == (1 if failure == "interrupt" else 2)
