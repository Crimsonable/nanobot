import json

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.bridge import BridgeChannel
from nanobot.config.schema import BridgeConfig


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


@pytest.mark.asyncio
async def test_bridge_inbound_message_preserves_session_and_metadata() -> None:
    channel = BridgeChannel(BridgeConfig(bridge_url="ws://bridge", allow_from=["user-1"]), MessageBus())

    await channel._handle_bridge_message(
        json.dumps(
            {
                "type": "inbound_message",
                "request_id": "req-1",
                "tenant_id": "tenant-a",
                "conversation_id": "conv-1",
                "session_key": "remote:tenant-a:conv-1",
                "sender_id": "user-1",
                "chat_id": "conv-1",
                "content": "hello",
                "attachments": ["/tmp/a.png"],
                "metadata": {"trace_id": "trace-1"},
            }
        )
    )

    msg = await channel.bus.consume_inbound()
    assert msg.sender_id == "user-1"
    assert msg.chat_id == "conv-1"
    assert msg.session_key == "remote:tenant-a:conv-1"
    assert msg.metadata["request_id"] == "req-1"
    assert msg.metadata["trace_id"] == "trace-1"
    assert msg.media == ["/tmp/a.png"]


@pytest.mark.asyncio
async def test_bridge_cancel_maps_to_stop_message() -> None:
    channel = BridgeChannel(BridgeConfig(bridge_url="ws://bridge", allow_from=["user-1"]), MessageBus())

    await channel._handle_bridge_message(
        json.dumps(
            {
                "type": "cancel",
                "request_id": "req-2",
                "tenant_id": "tenant-a",
                "conversation_id": "conv-2",
                "session_key": "remote:tenant-a:conv-2",
                "sender_id": "user-1",
            }
        )
    )

    msg = await channel.bus.consume_inbound()
    assert msg.content == "/stop"
    assert msg.session_key == "remote:tenant-a:conv-2"
    assert msg.sender_id == "user-1"


@pytest.mark.asyncio
async def test_bridge_send_encodes_progress_packet() -> None:
    channel = BridgeChannel(BridgeConfig(bridge_url="ws://bridge", allow_from=["*"]), MessageBus())
    channel._ws = _FakeWebSocket()
    channel._connected = True

    await channel.send(
        OutboundMessage(
            channel="bridge",
            chat_id="conv-1",
            content="thinking",
            metadata={
                "request_id": "req-3",
                "tenant_id": "tenant-a",
                "conversation_id": "conv-1",
                "_progress": True,
            },
        )
    )

    sent = json.loads(channel._ws.sent[0])
    assert sent["type"] == "progress"
    assert sent["request_id"] == "req-3"
    assert sent["conversation_id"] == "conv-1"
    assert sent["content"] == "thinking"


def test_bridge_channel_builds_register_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_SESSION_ID", "session-a")
    monkeypatch.setenv("BRIDGE_CONTAINER_NAME", "nanobot-session-a")

    channel = BridgeChannel(
        BridgeConfig(bridge_url="ws://bridge", bridge_token="secret", allow_from=["*"]),
        MessageBus(),
    )

    assert channel._build_handshake_packet() == {
        "type": "register",
        "version": 2,
        "session_id": "session-a",
        "container_name": "nanobot-session-a",
        "token": "secret",
    }

    monkeypatch.delenv("BRIDGE_SESSION_ID")
    monkeypatch.delenv("BRIDGE_CONTAINER_NAME")
