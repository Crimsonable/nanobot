from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from container_up import attachment_paths, feishu_im_tool, im_tools, qxt_im_tool


class FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        text: str = "{}",
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._text = text
        self._json = json_data or {}
        self.headers = headers or {}

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def text(self) -> str:
        return self._text

    async def json(self, content_type: str | None = None) -> dict[str, Any]:
        return self._json

    async def read(self) -> bytes:
        return self._text.encode("utf-8")


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self.responses.pop(0)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self.responses.pop(0)


class CapturingFeishuParser(feishu_im_tool.FeishuIMParser):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict[str, Any]] = []

    def _reply_message_sync(
        self,
        reply_target: dict[str, Any],
        message_type: str,
        content: str,
    ) -> dict[str, Any]:
        record = {
            "mode": "reply",
            "target": dict(reply_target),
            "message_type": message_type,
            "content": content,
        }
        self.sent.append(record)
        return record

    def _send_message_sync(
        self,
        reply_target: dict[str, Any],
        message_type: str,
        content: str,
    ) -> dict[str, Any]:
        record = {
            "mode": "send",
            "target": dict(reply_target),
            "message_type": message_type,
            "content": content,
        }
        self.sent.append(record)
        return record


@pytest.mark.asyncio
async def test_qxt_rejects_media_reference_for_another_frontend(monkeypatch):
    parser = im_tools.QxtIMParser(frontend_id="qxt-main")
    session = FakeSession([])
    monkeypatch.setattr(qxt_im_tool, "get_dispatch_session", lambda: session)
    with pytest.raises(RuntimeError, match="different frontend"):
        await parser._upload_attachment_with_retry(
            {"storage": "qxt", "frontend_id": "qxt-other", "media_id": "mid-1"}, "token"
        )
    assert session.calls == []


@pytest.mark.asyncio
async def test_qxt_upload_bytes_retries_without_workspace_or_minio(monkeypatch):
    parser = im_tools.QxtIMParser(frontend_id="qxt-main")
    parser.send_msg_url = "https://im.example.com/v2/message/send"
    parser.send_msg_retry_backoff = 0
    session = FakeSession(
        [
            FakeResponse(status=503),
            FakeResponse(json_data={"media_id": "mid-retried"}),
        ]
    )
    monkeypatch.setattr(qxt_im_tool, "get_dispatch_session", lambda: session)
    monkeypatch.setattr(parser, "get_access_token", lambda: "token")
    result = await parser.upload_outbound_attachment(
        data=b"pdf-bytes", filename="report.pdf", content_type="application/pdf"
    )
    assert result["media_id"] == "mid-retried"
    assert len(session.calls) == 2
    assert all(
        call["method"] == "POST" and call["url"].endswith("/v2/media/upload")
        for call in session.calls
    )
    for call in session.calls:
        assert call["data"]._fields[1][2] == b"pdf-bytes"


@pytest.mark.asyncio
async def test_post_message_with_url_attachment_uploads_then_sends_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = "https://files.example.com/report.txt"
    session = FakeSession(
        [
            FakeResponse(status=200, text='{"ok":true}'),
            FakeResponse(status=200, text="hello", headers={"Content-Type": "text/plain"}),
            FakeResponse(status=200, text='{"media_id":"mid-1"}', json_data={"media_id": "mid-1"}),
            FakeResponse(status=200, text='{"ok":true}'),
        ]
    )
    parser = im_tools.QxtIMParser()
    parser.send_msg_url = "https://im.example.com/v2/message/bot_send_to_conversation"

    monkeypatch.setattr(qxt_im_tool, "get_dispatch_session", lambda: session)
    monkeypatch.setattr(parser, "get_access_token", lambda: "token-1")

    result = await parser.post_message_with_retry(
        payload={
            "to_single_uid": "user-1",
            "type": "text",
            "message": {"content": "done"},
            "attachments": [str(attachment)],
        }
    )

    assert result["message"] == {"status": 200, "body": '{"ok":true}'}
    assert result["attachments"] == [
        {
            "attachment": str(attachment),
            "media_id": "mid-1",
            "response": {"status": 200, "body": '{"ok":true}'},
        }
    ]
    assert session.calls[0]["url"] == "https://im.example.com/v2/message/bot_send_to_conversation"
    assert session.calls[0]["json"] == {
        "to_single_uid": "user-1",
        "type": "text",
        "message": {"content": "done"},
    }
    assert session.calls[1]["method"] == "GET"
    assert session.calls[1]["url"] == attachment
    assert session.calls[2]["url"] == "https://im.example.com/v2/media/upload"
    assert session.calls[2]["params"] == {"access_token": "token-1"}
    assert session.calls[3]["url"] == "https://im.example.com/v2/message/bot_send_to_conversation"
    assert session.calls[3]["json"] == {
        "to_single_uid": "user-1",
        "type": "file",
        "message": {"media_id": "mid-1"},
    }


@pytest.mark.asyncio
async def test_feishu_post_message_renders_markdown_as_interactive_card() -> None:
    parser = CapturingFeishuParser()

    result = await parser.post_message_with_retry(
        payload={
            "content": "# Title\n\n- item\n\n```python\nprint('ok')\n```",
            "metadata": {
                "reply_target": {
                    "type": "feishu",
                    "receive_id_type": "chat_id",
                    "receive_id": "oc_1",
                    "message_id": "om_1",
                    "reply_in_thread": True,
                }
            },
        }
    )

    assert result["message"]["mode"] == "reply"
    assert result["message"]["message_type"] == "interactive"
    card = json.loads(result["message"]["content"])
    assert card["config"] == {"wide_screen_mode": True}
    assert any(element.get("tag") == "markdown" for element in card["elements"])
    assert any(element.get("tag") == "div" for element in card["elements"])


@pytest.mark.asyncio
async def test_feishu_post_message_renders_links_as_post() -> None:
    parser = CapturingFeishuParser()

    result = await parser.post_message_with_retry(
        payload={
            "content": "see [docs](https://example.com/docs)",
            "metadata": {
                "reply_target": {
                    "type": "feishu",
                    "receive_id_type": "open_id",
                    "receive_id": "ou_1",
                }
            },
        }
    )

    assert result["message"]["mode"] == "send"
    assert result["message"]["message_type"] == "post"
    body = json.loads(result["message"]["content"])
    assert body["zh_cn"]["content"][0] == [
        {"tag": "text", "text": "see "},
        {"tag": "a", "text": "docs", "href": "https://example.com/docs"},
    ]


@pytest.mark.asyncio
async def test_feishu_post_message_rebuilds_target_from_bridge_chat_id() -> None:
    parser = CapturingFeishuParser()

    result = await parser.post_message_with_retry(
        payload={
            "chat_id": "oc_1",
            "content": "done",
            "metadata": {"message_id": "om_1", "usr_id": "ou_1"},
        }
    )

    assert result["message"]["mode"] == "reply"
    assert result["message"]["message_type"] == "text"
    assert result["message"]["target"] == {
        "type": "feishu",
        "receive_id_type": "open_id",
        "receive_id": "ou_1",
        "message_id": "om_1",
        "thread_id": "",
        "reply_in_thread": False,
    }


def test_feishu_inbound_interactive_extracts_card_text() -> None:
    parser = feishu_im_tool.FeishuIMParser()
    message = type(
        "Message",
        (),
        {
            "message_type": "interactive",
            "message_id": "om_1",
            "content": json.dumps(
                {
                    "header": {"title": {"tag": "plain_text", "content": "Header"}},
                    "elements": [
                        {"tag": "markdown", "content": "**Hello**"},
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "Open"},
                            "url": "https://example.com",
                        },
                    ],
                }
            ),
        },
    )()

    attachments, content = parser._extract_inbound_content(
        user_id="ou_1",
        attachment_group="oc_1",
        message=message,
    )

    assert attachments == []
    assert content == "**Hello**\nOpen\nlink: https://example.com\ntitle: Header"


@pytest.mark.asyncio
async def test_post_message_skips_empty_text_and_sends_attachments_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession(
        [
            FakeResponse(status=200, text='{"media_id":"mid-2"}', json_data={"media_id": "mid-2"}),
            FakeResponse(status=200, text='{"ok":true}'),
        ]
    )
    parser = im_tools.QxtIMParser()
    parser.send_msg_url = "https://im.example.com/v2/message/bot_send_to_conversation"

    monkeypatch.setattr(qxt_im_tool, "get_dispatch_session", lambda: session)
    monkeypatch.setattr(parser, "get_access_token", lambda: "token-2")

    attachment = await parser.upload_outbound_attachment(
        data=b"png", filename="image.png", content_type="image/png"
    )
    assert attachment == {
        "storage": "qxt", "frontend_id": "default", "media_id": "mid-2",
        "upload_type": "image", "filename": "image.png", "content_type": "image/png",
    }

    result = await parser.post_message_with_retry(
        payload={
            "to_single_uid": "user-2",
            "type": "text",
            "message": {"content": ""},
            "attachments": [attachment],
        }
    )

    assert result["message"] is None
    assert len(result["attachments"]) == 1
    assert len(session.calls) == 2
    assert session.calls[0]["url"] == "https://im.example.com/v2/media/upload"
    assert session.calls[1]["json"] == {
        "to_single_uid": "user-2",
        "type": "file",
        "message": {"media_id": "mid-2"},
    }


@pytest.mark.asyncio
async def test_qxt_post_message_accepts_unified_payload_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession([FakeResponse(status=200, text='{"ok":true}')])
    parser = im_tools.QxtIMParser()
    parser.send_msg_url = "https://im.example.com/v2/message/bot_send_to_conversation"

    monkeypatch.setattr(qxt_im_tool, "get_dispatch_session", lambda: session)
    monkeypatch.setattr(parser, "get_access_token", lambda: "token-3")

    result = await parser.post_message_with_retry(
        payload={
            "chat_id": "ignored",
            "content": "done",
            "metadata": {
                "reply_target": {
                    "type": "qxt",
                    "to_single_uid": "user-3",
                }
            },
        }
    )

    assert result["message"] == {"status": 200, "body": '{"ok":true}'}
    assert session.calls[0]["json"] == {
        "to_single_uid": "user-3",
        "type": "text",
        "message": {"content": "done"},
    }


@pytest.mark.parametrize(
    ("event_type", "chat_type", "recipient"),
    [
        ("p2p_chat_receive_msg", "single", {"to_single_uid": "user-1"}),
        ("group_chat_at_bot", "group", {"to_group_id": "chat-1"}),
    ],
)
def test_qxt_subscribe_payload_is_normalized_to_unified_im_event(
    event_type: str, chat_type: str, recipient: dict[str, str],
) -> None:
    parser = im_tools.QxtIMParser()
    parser.token = "token-1"
    parser.appsecret = "0123456789abcdef"

    encrypted = parser.encrypt(
        text=json.dumps(
            {
                "event_type": event_type,
                "timestamp": "123456",
                "event": {
                    "sender_uid": "user-1",
                    "message": {
                        "chat_id": "chat-1",
                        "content": "hello",
                        "chat_type": chat_type,
                        "type": "text",
                        "message_id": "msg-1",
                    },
                },
            }
        ),
        token=parser.token,
        appsecret=parser.appsecret,
    )

    response, payload = parser.process_subscribe_form(
        type(
            "SubForm",
            (),
            encrypted,
        )()
    )

    assert payload == {
        "event_type": "im_message_receive",
        "event": {
            "chat_id": "chat-1",
            "usr_id": "user-1",
            "content": "hello",
            "attachments": [],
            "metadata": {
                "provider": "qxt",
                "frontend_id": "default",
                "event_type": event_type,
                "chat_type": chat_type,
                "message_type": "text",
                "message_id": "msg-1",
                "timestamp": "123456",
                "source": "subscribe",
                "reply_target": {
                    "type": "qxt",
                    "frontend_id": "default",
                    **recipient,
                },
            },
        },
    }
    assert response["encrypt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("direct_payload", [False, True])
async def test_qxt_group_reply_sends_text_and_attachment_to_group(
    monkeypatch: pytest.MonkeyPatch, direct_payload: bool,
) -> None:
    parser = im_tools.QxtIMParser(frontend_id="qxt-main")
    parser.send_msg_url = "https://im.example.com/v2/message/bot_send_to_conversation"
    session = FakeSession([FakeResponse(), FakeResponse()])
    monkeypatch.setattr(qxt_im_tool, "get_dispatch_session", lambda: session)
    monkeypatch.setattr(parser, "get_access_token", lambda: "token")
    normalized = parser.normalize_subscribe_payload({
        "corp_id": "14777504996792969",
        "appid": "14781834433937184",
        "event_type": "group_chat_at_bot",
        "timestamp": "1791788165",
        "event": {
            "sender_account": "2021088",
            "sender_uid": "14777504996831943",
            "message": {
                "chat_id": "14781834433951752",
                "chat_type": "group",
                "type": "text",
                "content": "@智成安监\u2004你好",
                "at": [{"name": "智成安监", "account": "bot-SJGI98El", "uid": "14781834433937184"}],
                "message_id": "7695671565739884551",
            },
        },
    })
    event = normalized["event"]
    assert event["usr_id"] == "14777504996831943"
    assert event["content"] == "@智成安监\u2004你好"
    payload = {
        "chat_id": event["chat_id"],
        "content": "你好",
        "metadata": event["metadata"],
    }
    if direct_payload:
        payload = {
            "to_group_id": event["chat_id"],
            "type": "text",
            "message": {"content": "你好"},
        }
    payload["attachments"] = [{
        "storage": "qxt", "frontend_id": "qxt-main", "media_id": "group-file",
    }]

    await parser.post_message_with_retry(payload=payload)

    assert [call["json"] for call in session.calls] == [
        {"to_group_id": "14781834433951752", "type": "text", "message": {"content": "你好"}},
        {"to_group_id": "14781834433951752", "type": "file", "message": {"media_id": "group-file"}},
    ]


@pytest.mark.asyncio
async def test_qxt_prepare_inbound_event_downloads_content_url_into_instance_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession(
        [
            FakeResponse(
                status=200,
                text="file-bytes",
                headers={"Content-Disposition": 'attachment; filename="report.pdf"'},
            )
        ]
    )
    parser = im_tools.QxtIMParser()

    monkeypatch.setattr(qxt_im_tool, "get_dispatch_session", lambda: session)
    monkeypatch.setattr(attachment_paths, "HOST_WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(attachment_paths, "CHILD_WORKSPACE_TARGET", "/app/nanobot_workspaces")
    monkeypatch.setattr(
        "container_up.attachments.ATTACHMENT_URL_PREFIX",
        "https://files.example.com/",
    )
    prepared = await parser.prepare_inbound_event(
        {
            "event_type": "im_message_receive",
            "event": {
                "chat_id": "conv-1",
                "usr_id": "user-1",
                "content": "https://files.example.com/report.pdf",
                "attachments": [],
                "metadata": {
                    "provider": "qxt",
                    "message_id": "msg-1",
                },
            },
        }
    )

    event = prepared["event"]
    attachments = event["attachments"]
    assert "org_id" not in event
    assert event["metadata"]["attachments_materialized"] is True
    assert len(attachments) == 1
    local_path = Path(attachments[0])
    assert local_path.is_absolute()
    assert local_path.is_relative_to(Path("/app/nanobot_workspaces/default/user-1").resolve())
    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"] == "https://files.example.com/report.pdf"

    host_workspace = attachment_paths.host_instance_workspace_path(
        "user-1",
        frontend_id="default",
    )
    host_file = (
        tmp_path
        / host_workspace.relative_to(tmp_path)
        / "cache"
        / "attachments"
        / "qxt"
        / attachment_paths.safe_instance_name("conv-1")
        / local_path.name
    )
    assert host_file.is_file()
    assert host_file.read_bytes() == b"file-bytes"
