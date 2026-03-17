import asyncio

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

    task = asyncio.create_task(
        hub.submit_message(
            org_id="org-a",
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

    await hub.handle_child_packet(
        "org-a",
        {
            "type": "progress",
            "request_id": "req-1",
            "conversation_id": "conv-1",
            "tenant_id": "tenant-a",
            "content": "thinking",
        },
    )
    await hub.handle_child_packet(
        "org-a",
        {
            "type": "final",
            "request_id": "req-1",
            "conversation_id": "conv-1",
            "tenant_id": "tenant-a",
            "content": "done",
        },
    )

    result = await task
    assert child.sent[1] == {
        "type": "inbound_message",
        "version": PROTOCOL_VERSION,
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
    assert [event["type"] for event in result["events"]] == ["progress", "final"]
    assert result["org_id"] == "org-a"
    assert result["result"]["content"] == "done"


@pytest.mark.asyncio
async def test_same_request_id_across_orgs_does_not_collide() -> None:
    hub = BridgeHub()
    child_a = _FakeChild()
    child_b = _FakeChild()

    await hub.register_child(
        child_a,
        build_register_packet(org_id="org-a", container_name="nanobot-org-a"),
    )
    await hub.register_child(
        child_b,
        build_register_packet(org_id="org-b", container_name="nanobot-org-b"),
    )

    task_a = asyncio.create_task(
        hub.submit_message(
            org_id="org-a",
            conversation_id="conv-a",
            user_id="user-a",
            tenant_id="tenant-a",
            content="hello a",
            attachments=[],
            metadata={},
            request_id="req-1",
            timeout=1,
        )
    )
    task_b = asyncio.create_task(
        hub.submit_message(
            org_id="org-b",
            conversation_id="conv-b",
            user_id="user-b",
            tenant_id="tenant-b",
            content="hello b",
            attachments=[],
            metadata={},
            request_id="req-1",
            timeout=1,
        )
    )
    await asyncio.sleep(0)

    await hub.handle_child_packet(
        "org-b",
        {
            "type": "final",
            "request_id": "req-1",
            "conversation_id": "conv-b",
            "tenant_id": "tenant-b",
            "content": "done b",
        },
    )
    await hub.handle_child_packet(
        "org-a",
        {
            "type": "final",
            "request_id": "req-1",
            "conversation_id": "conv-a",
            "tenant_id": "tenant-a",
            "content": "done a",
        },
    )

    result_a = await task_a
    result_b = await task_b
    assert result_a["result"]["content"] == "done a"
    assert result_b["result"]["content"] == "done b"


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
        conversation_id="conv-2",
        user_id="user-1",
        tenant_id="tenant-a",
        request_id="req-2",
    )

    assert result == {
        "status": "accepted",
        "org_id": "org-a",
        "request_id": "req-2",
        "conversation_id": "conv-2",
        "tenant_id": "tenant-a",
    }
    assert child.sent[1] == {
        "type": "cancel",
        "version": PROTOCOL_VERSION,
        "request_id": "req-2",
        "tenant_id": "tenant-a",
        "conversation_id": "conv-2",
        "session_key": "remote:tenant-a:conv-2",
        "sender_id": "user-1",
    }


@pytest.mark.asyncio
async def test_submit_message_timeout_sends_cancel() -> None:
    hub = BridgeHub()
    child = _FakeChild()
    await hub.register_child(
        child,
        build_register_packet(org_id="org-a", container_name="nanobot-org-a"),
    )

    with pytest.raises(asyncio.TimeoutError):
        await hub.submit_message(
            org_id="org-a",
            conversation_id="conv-timeout",
            user_id="user-1",
            tenant_id="tenant-a",
            content="hello",
            attachments=[],
            metadata={},
            request_id="req-timeout",
            timeout=0.01,
        )

    assert child.sent[1]["type"] == "inbound_message"
    assert child.sent[2] == {
        "type": "cancel",
        "version": PROTOCOL_VERSION,
        "request_id": "req-timeout",
        "tenant_id": "tenant-a",
        "conversation_id": "conv-timeout",
        "session_key": "remote:tenant-a:conv-timeout",
        "sender_id": "user-1",
    }


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
