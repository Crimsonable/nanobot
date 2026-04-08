import json

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.bridge import BridgeChannel, BridgeConfig


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


class _FailingWebSocket:
    async def send(self, data: str) -> None:
        raise RuntimeError("send failed")


@pytest.mark.asyncio
async def test_bridge_inbound_message_preserves_session_and_metadata() -> None:
    channel = BridgeChannel(BridgeConfig(bridge_url="ws://bridge", allow_from=["user-1"]), MessageBus())

    await channel._handle_bridge_message(
        json.dumps(
            {
                "type": "inbound_message",
                "session_key": "remote:conv-1",
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
    assert msg.session_key == "remote:conv-1"
    assert msg.metadata["trace_id"] == "trace-1"
    assert msg.media == ["/tmp/a.png"]


@pytest.mark.asyncio
async def test_bridge_cancel_maps_to_stop_message() -> None:
    channel = BridgeChannel(BridgeConfig(bridge_url="ws://bridge", allow_from=["user-1"]), MessageBus())

    await channel._handle_bridge_message(
        json.dumps(
            {
                "type": "cancel",
                "session_key": "remote:conv-2",
                "sender_id": "user-1",
                "chat_id": "conv-2",
            }
        )
    )

    msg = await channel.bus.consume_inbound()
    assert msg.content == "/stop"
    assert msg.session_key == "remote:conv-2"
    assert msg.sender_id == "user-1"


@pytest.mark.asyncio
async def test_bridge_send_encodes_outbound_packet() -> None:
    channel = BridgeChannel(BridgeConfig(bridge_url="ws://bridge", allow_from=["*"]), MessageBus())
    channel._ws = _FakeWebSocket()
    channel._connected = True

    await channel.send(
        OutboundMessage(
            channel="bridge",
            chat_id="conv-1",
            content="thinking",
            metadata={
                "_progress": True,
                "trace_id": "trace-3",
            },
        )
    )

    sent = json.loads(channel._ws.sent[0])
    assert sent["type"] == "outbound_message"
    assert sent["channel"] == "bridge"
    assert sent["chat_id"] == "conv-1"
    assert sent["content"] == "thinking"
    assert sent["metadata"] == {"_progress": True, "trace_id": "trace-3"}


@pytest.mark.asyncio
async def test_bridge_send_raises_on_websocket_failure() -> None:
    channel = BridgeChannel(BridgeConfig(bridge_url="ws://bridge", allow_from=["*"]), MessageBus())
    channel._ws = _FailingWebSocket()
    channel._connected = True

    with pytest.raises(RuntimeError, match="send failed"):
        await channel.send(OutboundMessage(channel="bridge", chat_id="conv-1", content="x"))


@pytest.mark.asyncio
async def test_bridge_send_delta_forwards_stream_packet() -> None:
    channel = BridgeChannel(BridgeConfig(bridge_url="ws://bridge", allow_from=["*"]), MessageBus())
    channel._ws = _FakeWebSocket()
    channel._connected = True

    await channel.send_delta(
        "conv-1",
        "partial",
        {"_stream_delta": True, "_stream_id": "seg-1"},
    )

    sent = json.loads(channel._ws.sent[0])
    assert sent["type"] == "outbound_message"
    assert sent["chat_id"] == "conv-1"
    assert sent["content"] == "partial"
    assert sent["metadata"] == {"_stream_delta": True, "_stream_id": "seg-1"}


def test_bridge_send_keeps_attachment_refs_as_is() -> None:
    channel = BridgeChannel(BridgeConfig(bridge_url="ws://bridge", allow_from=["*"]), MessageBus())

    packet = channel._encode_outbound(
        OutboundMessage(
            channel="bridge",
            chat_id="conv-1",
            content="done",
            media=["/app/nanobot_workspaces/user-a/report 1.png"],
        )
    )

    assert packet["attachments"] == ["/app/nanobot_workspaces/user-a/report 1.png"]


@pytest.mark.asyncio
async def test_bridge_send_proactive_message_includes_attachments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PARENT_BRIDGE_URL", "ws://bridge/ws/bridge")
    monkeypatch.setenv("BRIDGE_ORG_ID", "org-1")

    captured: dict[str, object] = {}

    def _fake_post(url: str, token: str, payload: dict[str, object]) -> None:
        captured["url"] = url
        captured["token"] = token
        captured["payload"] = payload

    monkeypatch.setattr(BridgeChannel, "_post_outbound_sync", staticmethod(_fake_post))

    await BridgeChannel.send_proactive_message(
        BridgeConfig(bridge_url="ws://bridge", bridge_token="secret", allow_from=["*"]),
        to="user-1:::conv-1",
        content="done",
        media=["/app/nanobot_workspaces/user-a/a.png"],
        metadata={"trace_id": "trace-1"},
    )

    assert captured["url"] == "http://bridge/api/bridge/outbound"
    assert captured["token"] == "secret"
    assert captured["payload"] == {
        "org_id": "org-1",
        "to": "user-1:::conv-1",
        "content": "done",
        "attachments": ["/app/nanobot_workspaces/user-a/a.png"],
        "metadata": {"trace_id": "trace-1"},
    }


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


def test_bridge_channel_applies_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_URL_OVERRIDE", "ws://127.0.0.1:37237")
    monkeypatch.setenv("BRIDGE_TOKEN_OVERRIDE", "local-secret")
    monkeypatch.setenv("BRIDGE_ALLOW_FROM_OVERRIDE", "*")

    channel = BridgeChannel(
        BridgeConfig(bridge_url="ws://bridge", bridge_token="shared", allow_from=["user-1"]),
        MessageBus(),
    )

    assert channel.config.bridge_url == "ws://127.0.0.1:37237"
    assert channel.config.bridge_token == "local-secret"
    assert channel.config.allow_from == ["*"]
