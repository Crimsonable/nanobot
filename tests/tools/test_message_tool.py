import os
from pathlib import Path

import pytest

from nanobot.agent.tools.message import MessageTool
from nanobot.bus.events import OutboundMessage
from nanobot.config.paths import get_workspace_path


@pytest.mark.asyncio
async def test_message_tool_returns_error_when_no_target_context() -> None:
    tool = MessageTool()
    result = await tool.execute(content="test")
    assert result == "Error: No target channel/chat specified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "not a list",
        [["ok"], "row-not-a-list"],
        [["ok", 42]],
        [[None]],
    ],
)
async def test_message_tool_rejects_malformed_buttons(bad) -> None:
    """``buttons`` must be ``list[list[str]]``; the tool validates the shape
    up front so a malformed LLM payload errors visibly instead of slipping
    into the channel layer where Telegram would silently reject the frame."""
    tool = MessageTool()
    result = await tool.execute(
        content="hi", channel="telegram", chat_id="1", buttons=bad,
    )
    assert result == "Error: buttons must be a list of list of strings"


@pytest.mark.asyncio
async def test_message_tool_marks_channel_delivery_only_when_enabled() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)

    await tool.execute(content="normal", channel="telegram", chat_id="1")
    token = tool.set_record_channel_delivery(True)
    try:
        await tool.execute(content="cron", channel="telegram", chat_id="1")
    finally:
        tool.reset_record_channel_delivery(token)

    assert sent[0].metadata == {}
    assert sent[1].metadata == {"_record_channel_delivery": True}


@pytest.mark.asyncio
async def test_message_tool_inherits_metadata_for_same_target() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)
    slack_meta = {"slack": {"thread_ts": "111.222", "channel_type": "channel"}}
    tool.set_context("slack", "C123", metadata=slack_meta)

    await tool.execute(content="thread reply")

    assert sent[0].metadata == slack_meta


@pytest.mark.asyncio
async def test_message_tool_does_not_inherit_metadata_for_cross_target() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)
    tool.set_context(
        "slack",
        "C123",
        metadata={"slack": {"thread_ts": "111.222", "channel_type": "channel"}},
    )

    await tool.execute(content="channel reply", channel="slack", chat_id="C999")

    assert sent[0].metadata == {}


@pytest.mark.asyncio
async def test_message_tool_resolves_relative_media_paths() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)

    await tool.execute(
        content="see attached",
        channel="telegram",
        chat_id="1",
        media=["output/image.png"],
    )

    expected = str(get_workspace_path() / "output/image.png")
    assert sent[0].media == [expected]


@pytest.mark.asyncio
async def test_message_tool_resolves_relative_media_paths_from_active_workspace(tmp_path) -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    workspace = tmp_path / "workspace"
    tool = MessageTool(send_callback=_send, workspace=workspace)

    await tool.execute(
        content="see attached",
        channel="telegram",
        chat_id="1",
        media=["output/image.png"],
    )

    assert sent[0].media == [str(workspace / "output/image.png")]


@pytest.mark.asyncio
async def test_message_tool_passes_through_absolute_media_paths() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)

    abs_path = os.path.abspath(os.path.join(os.sep, "tmp", "abs_image.png"))

    await tool.execute(
        content="see attached",
        channel="telegram",
        chat_id="1",
        media=[abs_path],
    )

    assert sent[0].media == [abs_path]


@pytest.mark.asyncio
async def test_message_tool_passes_through_url_media_paths() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)

    url = "https://example.com/image.png"

    await tool.execute(
        content="see attached",
        channel="telegram",
        chat_id="1",
        media=[url],
    )

    assert sent[0].media == [url]


@pytest.mark.asyncio
async def test_message_tool_resolves_mixed_media_paths() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)

    abs_path = os.path.abspath(os.path.join(os.sep, "tmp", "absolute.png"))

    await tool.execute(
        content="see attached",
        channel="telegram",
        chat_id="1",
        media=[
            "output/relative.png",
            abs_path,
            "https://example.com/url.png",
            "http://example.com/http.png",
        ],
    )

    expected_relative = str(get_workspace_path() / "output/relative.png")
    assert sent[0].media == [
        expected_relative,
        abs_path,
        "https://example.com/url.png",
        "http://example.com/http.png",
    ]


@pytest.mark.asyncio
async def test_message_tool_uploads_local_bridge_media_via_container_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    attachment = tmp_path / "report.pdf"
    attachment.write_text("hello", encoding="utf-8")

    monkeypatch.setattr(
        "nanobot.utils.document._upload_local_attachment_ref",
        lambda path, metadata=None: f"https://files.example/{path.name}",
    )

    tool = MessageTool(send_callback=_send)
    tool.set_context(
        "bridge",
        "chat-1",
        metadata={"frontend_id": "web-main", "usr_id": "user-1"},
    )

    result = await tool.execute(content="see attached", media=[str(attachment)])

    assert result == "Message sent to bridge:chat-1 with 1 attachments"
    assert sent[0].media == [
        {
            "url": "https://files.example/report.pdf",
            "filename": "report.pdf",
            "content_type": "application/pdf",
        }
    ]


@pytest.mark.asyncio
async def test_message_tool_normalizes_remote_bridge_media_to_attachment_objects() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)
    tool.set_context(
        "bridge",
        "chat-1",
        metadata={"frontend_id": "web-main", "usr_id": "user-1"},
    )

    url = "https://example.com/file.pdf"
    await tool.execute(content="see attached", media=[url])

    assert sent[0].media == [
        {
            "url": url,
            "filename": "file.pdf",
            "content_type": "application/pdf",
        }
    ]


@pytest.mark.asyncio
async def test_message_tool_uses_extension_fallback_for_remote_bridge_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    def fake_guess_type(_url: str, strict: bool = False):
        return (None, None)

    monkeypatch.setattr(
        "nanobot.agent.tools.message.mimetypes.guess_type",
        fake_guess_type,
    )

    tool = MessageTool(send_callback=_send)
    tool.set_context(
        "bridge",
        "chat-1",
        metadata={"frontend_id": "web-main", "usr_id": "user-1"},
    )

    url = "https://example.com/report.docx"
    await tool.execute(content="see attached", media=[url])

    assert sent[0].media == [
        {
            "url": url,
            "filename": "report.docx",
            "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
    ]


@pytest.mark.asyncio
async def test_message_tool_reports_bridge_upload_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    attachment = tmp_path / "report.pdf"
    attachment.write_text("hello", encoding="utf-8")

    monkeypatch.setattr(
        "nanobot.utils.document._upload_local_attachment_ref",
        lambda path, metadata=None: None,
    )

    tool = MessageTool(send_callback=_send)
    tool.set_context(
        "bridge",
        "chat-1",
        metadata={"frontend_id": "web-main", "usr_id": "user-1"},
    )

    result = await tool.execute(content="see attached", media=[str(attachment)])

    assert result.startswith("Error sending message: failed to upload attachment via container_up:")
    assert sent == []
