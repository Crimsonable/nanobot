import pytest

from nanobot.agent.tools.message import MessageTool


@pytest.mark.asyncio
async def test_message_tool_returns_error_when_no_target_context() -> None:
    tool = MessageTool()
    result = await tool.execute(content="test")
    assert result == "Error: No target channel/chat specified"


@pytest.mark.asyncio
async def test_message_tool_maps_current_bridge_conversation_id_to_delivery_target() -> None:
    sent = []

    async def _send(msg):
        sent.append(msg)

    tool = MessageTool(
        send_callback=_send,
        default_channel="bridge",
        default_chat_id="user-1:::oc_current",
        default_message_id="msg-1",
        default_metadata={"frontend_id": "feishu-main"},
    )

    result = await tool.execute(
        content="report",
        channel="bridge",
        chat_id="oc_current",
        media=["/tmp/report.pdf"],
    )

    assert result == "Message sent to bridge:user-1:::oc_current with 1 attachments"
    assert sent[0].chat_id == "user-1:::oc_current"
    assert sent[0].metadata == {"frontend_id": "feishu-main", "message_id": "msg-1"}


@pytest.mark.asyncio
async def test_message_tool_keeps_non_current_bridge_chat_id() -> None:
    sent = []

    async def _send(msg):
        sent.append(msg)

    tool = MessageTool(
        send_callback=_send,
        default_channel="bridge",
        default_chat_id="user-1:::oc_current",
        default_message_id="msg-1",
        default_metadata={"frontend_id": "feishu-main"},
    )

    result = await tool.execute(
        content="report",
        channel="bridge",
        chat_id="oc_other",
        media=["/tmp/report.pdf"],
    )

    assert result == "Message sent to bridge:oc_other with 1 attachments"
    assert sent[0].chat_id == "oc_other"
    assert sent[0].metadata == {}
