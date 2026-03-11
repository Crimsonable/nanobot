import asyncio

import pytest

from bridge_service.core import BridgeCore


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed: tuple[int, str] | None = None

    async def send_json(self, packet: dict) -> None:
        self.sent.append(packet)

    async def close(self, *, code: int, reason: str) -> None:
        self.closed = (code, reason)


@pytest.mark.asyncio
async def test_submit_message_routes_to_bot_and_waits_for_final() -> None:
    core = BridgeCore()
    bot = _FakeBot()
    core.register_bot(bot)

    task = asyncio.create_task(
        core.submit_message(
            conversation_id="conv-1",
            user_id="user-1",
            tenant_id="tenant-a",
            content="hello",
            attachments=["/tmp/a.png"],
            metadata={"trace_id": "trace-1"},
            request_id="req-1",
            timeout=1,
        )
    )
    await asyncio.sleep(0)

    await core.handle_bot_packet(
        {
            "type": "progress",
            "request_id": "req-1",
            "conversation_id": "conv-1",
            "tenant_id": "tenant-a",
            "content": "thinking",
        }
    )
    await core.handle_bot_packet(
        {
            "type": "final",
            "request_id": "req-1",
            "conversation_id": "conv-1",
            "tenant_id": "tenant-a",
            "content": "done",
        }
    )

    result = await task
    assert bot.sent == [
        {
            "type": "inbound_message",
            "version": 1,
            "request_id": "req-1",
            "tenant_id": "tenant-a",
            "conversation_id": "conv-1",
            "session_key": "remote:tenant-a:conv-1",
            "channel": "bridge",
            "sender_id": "user-1",
            "chat_id": "conv-1",
            "content": "hello",
            "attachments": ["/tmp/a.png"],
            "metadata": {"trace_id": "trace-1"},
        }
    ]
    assert [event["type"] for event in result["events"]] == ["progress", "final"]
    assert result["result"]["content"] == "done"


@pytest.mark.asyncio
async def test_submit_cancel_routes_to_bot() -> None:
    core = BridgeCore()
    bot = _FakeBot()
    core.register_bot(bot)

    result = await core.submit_cancel(
        conversation_id="conv-2",
        user_id="user-1",
        tenant_id="tenant-a",
        request_id="req-2",
    )

    assert result["status"] == "accepted"
    assert bot.sent == [
        {
            "type": "cancel",
            "version": 1,
            "request_id": "req-2",
            "tenant_id": "tenant-a",
            "conversation_id": "conv-2",
            "session_key": "remote:tenant-a:conv-2",
            "sender_id": "user-1",
        }
    ]


@pytest.mark.asyncio
async def test_authenticate_ws_rejects_invalid_token() -> None:
    core = BridgeCore(token="secret")
    bot = _FakeBot()

    ok = await core.authenticate_ws(bot, {"type": "auth", "token": "wrong"})

    assert ok is False
    assert bot.closed == (4003, "invalid token")
