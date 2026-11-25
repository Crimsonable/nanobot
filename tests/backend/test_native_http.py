import httpx
import pytest

from agent_backend.native_sse import messages
from agent_backend.opencode import OpenCode


@pytest.mark.asyncio
async def test_paginated_history_uses_cursor_without_repeating_order():
    queries = []

    def response(request):
        queries.append(dict(request.url.params))
        if "cursor" not in request.url.params:
            return httpx.Response(
                200,
                json={"data": [{"id": "answer", "type": "assistant"}], "cursor": {"next": "old"}},
            )
        return httpx.Response(200, json={"data": [{"id": "input", "type": "user"}], "cursor": {}})

    async with httpx.AsyncClient(
        base_url="http://native", transport=httpx.MockTransport(response)
    ) as http:
        assert [m["id"] for m in await messages(http, "s", message_id="input")] == [
            "input",
            "answer",
        ]
    assert queries == [{"order": "desc", "limit": "200"}, {"limit": "200", "cursor": "old"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [False, True])
@pytest.mark.parametrize("same", [False, True])
async def test_conflict_requires_matching_persisted_native_input(delivered, same):
    metadata = {"request_id": "r", "deadline_at": 123}
    existing = {
        "id": "input",
        "metadata": {"managed": metadata if same else {"request_id": "foreign"}},
    }

    def response(request):
        if request.method == "POST":
            return httpx.Response(409)
        if request.url.path.endswith("/inbox"):
            return httpx.Response(
                200, json={"data": [] if delivered else [{"id": "input", "payload": existing}]}
            )
        return httpx.Response(200, json={"data": [existing], "cursor": {}})

    native = OpenCode({"url": "http://native", "password": "test"})
    await native.http.aclose()
    native.http = httpx.AsyncClient(
        base_url="http://native", transport=httpx.MockTransport(response)
    )
    try:
        if same:
            await native.submit({"session_id": "s", "message_id": "input"}, "hi", metadata)
        else:
            with pytest.raises(httpx.HTTPStatusError):
                await native.submit({"session_id": "s", "message_id": "input"}, "hi", metadata)
    finally:
        await native.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timeline", [[], [{"id": "input", "type": "user"}, {"id": "next", "type": "user"}]]
)
async def test_queued_or_old_input_cancel_does_not_interrupt_current_turn(timeline):
    calls = []

    def response(request):
        calls.append((request.method, request.url.path))
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"data": list(reversed(timeline)), "cursor": {}})

    native = OpenCode({"url": "http://native", "password": "test"})
    await native.http.aclose()
    native.http = httpx.AsyncClient(
        base_url="http://native", transport=httpx.MockTransport(response)
    )
    try:
        await native.cancel_input({"session_id": "s", "message_id": "input"})
        assert calls == [
            ("DELETE", "/api/session/s/inbox/input"),
            ("GET", "/api/session/s/message"),
        ]
    finally:
        await native.close()


@pytest.mark.asyncio
async def test_interrupt_always_waits_for_native_cleanup():
    calls = []

    def response(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"interrupted": False})

    native = OpenCode({"url": "http://native", "password": "test"})
    await native.http.aclose()
    native.http = httpx.AsyncClient(
        base_url="http://native", transport=httpx.MockTransport(response)
    )
    try:
        await native.interrupt("s")
        assert calls == ["/api/session/s/interrupt", "/api/experimental/session/s/wait"]
    finally:
        await native.close()
