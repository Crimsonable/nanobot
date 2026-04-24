import pytest

from container_up.bridge_hub import BridgeHub
from container_up.bridge_protocol import PROTOCOL_VERSION, build_register_packet


class _FakeChild:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed: tuple[int, str] | None = None

    async def send_json(self, packet: dict) -> None:
        self.sent.append(packet)

    async def close(self, *, code: int, reason: str) -> None:
        self.closed = (code, reason)


@pytest.mark.asyncio
async def test_register_child_and_submit_message_are_org_bound() -> None:
    hub = BridgeHub(token="secret")
    child = _FakeChild()

    org_id = await hub.register_child(
        child,
        build_register_packet(
            org_id="org-a",
            container_name="nanobot-org-a",
            token="secret",
        ),
    )

    assert org_id == "org-a"
    assert child.sent[0] == {
        "type": "register_ok",
        "version": PROTOCOL_VERSION,
        "org_id": "org-a",
        "container_name": "nanobot-org-a",
    }

    result = await hub.submit_message(
        org_id="org-a",
        chat_id="chat-1",
        usr_id="user-1",
        content="hello",
        attachments=["/tmp/a.png"],
        metadata={"trace_id": "trace-1"},
    )

    assert child.sent[1] == {
        "type": "inbound_message",
        "version": PROTOCOL_VERSION,
        "channel": "bridge",
        "chat_id": "chat-1",
        "content": "hello",
        "attachments": ["/tmp/a.png"],
        "metadata": {
            "trace_id": "trace-1",
            "usr_id": "user-1",
        },
    }
    assert result == {
        "status": "accepted",
        "org_id": "org-a",
        "chat_id": "chat-1",
    }


@pytest.mark.asyncio
async def test_submit_cancel_routes_to_bound_org() -> None:
    hub = BridgeHub()
    child = _FakeChild()
    await hub.register_child(
        child,
        build_register_packet(org_id="org-a", container_name="nanobot-org-a"),
    )

    result = await hub.submit_cancel(
        org_id="org-a",
        chat_id="chat-2",
        usr_id="user-1",
    )

    assert result == {
        "status": "accepted",
        "org_id": "org-a",
        "chat_id": "chat-2",
    }
    assert child.sent[1] == {
        "type": "cancel",
        "version": PROTOCOL_VERSION,
        "channel": "bridge",
        "chat_id": "chat-2",
        "metadata": {
            "usr_id": "user-1",
        },
    }


@pytest.mark.asyncio
async def test_handle_child_packet_returns_packet() -> None:
    hub = BridgeHub()
    child = _FakeChild()
    await hub.register_child(
        child,
        build_register_packet(org_id="org-a", container_name="nanobot-org-a"),
    )

    packet = {
        "type": "outbound_message",
        "chat_id": "chat-3",
        "content": "done",
        "metadata": {},
    }
    assert await hub.handle_child_packet("org-a", packet) == packet


@pytest.mark.asyncio
async def test_register_child_rejects_invalid_token() -> None:
    hub = BridgeHub(token="secret")
    child = _FakeChild()

    org_id = await hub.register_child(
        child,
        build_register_packet(
            org_id="org-a",
            container_name="nanobot-org-a",
            token="wrong",
        ),
    )

    assert org_id is None
    assert child.closed == (4003, "invalid token")
