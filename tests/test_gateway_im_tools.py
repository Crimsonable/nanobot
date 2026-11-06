from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from typing import Any

import pytest

from channel_gateway import feishu_im_tool
from channel_gateway import qxt_im_tool as im_tools
from channel_gateway import qxt_im_tool


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
        super().__init__(frontend_id="feishu", frontend_config={})
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
async def test_feishu_post_message_renders_markdown_as_interactive_card() -> None:
    parser = CapturingFeishuParser()

    result = await parser.post_message(
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

    result = await parser.post_message(
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


def test_feishu_inbound_interactive_extracts_card_text() -> None:
    parser = feishu_im_tool.FeishuIMParser(frontend_id="feishu", frontend_config={})
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
async def test_qxt_post_message_accepts_unified_payload_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession([FakeResponse(status=200, text='{"ok":true}')])
    parser = im_tools.QxtIMParser(frontend_id="qxt", frontend_config={})
    parser.send_msg_url = "https://im.example.com/v2/message/bot_send_to_conversation"

    monkeypatch.setattr(qxt_im_tool, "get_dispatch_session", lambda: session)
    monkeypatch.setattr(parser, "get_access_token", AsyncMock(return_value="token-3"))

    result = await parser.post_message(
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
    "token_config",
    [{}, {"callback_token": ""}, {"callback_token": None}, {"callback_token": "token-1"}],
)
def test_qxt_callback_token_is_optional(token_config: dict[str, Any]) -> None:
    parser = im_tools.QxtIMParser(
        frontend_id="qxt",
        frontend_config={
            "app_id": "app-1",
            "app_secret": "0123456789abcdef",
            "corp_id": "corp-1",
            "access_url": "https://im.example.com/access_token",
            "send_msg_url": "https://im.example.com/send",
            **token_config,
        },
    )
    parser.start()
    token = token_config.get("callback_token") or ""
    encrypted = parser.encrypt(text=json.dumps({"event_type": "check_url"}), token=token)
    response, payload = parser.process_subscribe_form(type("SubForm", (), encrypted)())
    assert payload is None
    assert parser.decrypt(
        signature=response["msgSignature"], timeStamp=response["timeStamp"],
        nonce=response["nonce"], encrypt=response["encrypt"], token=token,
    ) == "success"
    with pytest.raises(ValueError, match="Signature verification error"):
        parser.process_subscribe_form(
            type("SubForm", (), {**encrypted, "msgSignature": "invalid"})()
        )


@pytest.mark.parametrize("callback_token", ["", "token-1"])
@pytest.mark.parametrize(
    ("event_type", "chat_type", "recipient"),
    [
        ("p2p_chat_receive_msg", "single", {"to_single_uid": "user-1"}),
        ("group_chat_at_bot", "group", {"to_group_id": "chat-1"}),
    ],
)
def test_qxt_subscribe_payload_is_normalized_to_unified_im_event(
    event_type: str, chat_type: str, recipient: dict[str, str], callback_token: str,
) -> None:
    parser = im_tools.QxtIMParser(frontend_id="qxt", frontend_config={})
    parser.token = callback_token
    parser.appsecret = "0123456789abcdef"

    encrypted = parser.encrypt(
        text=json.dumps(
            {
                "event_type": event_type,
                "timestamp": "123456",
                "event": {
                    "sender_uid": "user-1",
                    "sender_account": "account-1",
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
            "user_id": "account-1",
            "content": "hello",
            "attachments": [],
            "metadata": {
                "provider": "qxt",
                "frontend_id": "qxt",
                "event_type": event_type,
                "chat_type": chat_type,
                "message_type": "text",
                "message_id": "msg-1",
                "timestamp": "123456",
                "source": "subscribe",
                "reply_target": {
                    "type": "qxt",
                    "frontend_id": "qxt",
                    **recipient,
                },
            },
        },
    }
    assert response["encrypt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("recipient", [{"to_single_uid": "user-1"}, {"to_group_id": "group-1"}])
@pytest.mark.parametrize("content", ["", "report ready"])
async def test_qxt_sends_uploaded_media_without_reupload(monkeypatch, recipient, content):
    parser = im_tools.QxtIMParser(frontend_id="qxt", frontend_config={"send_msg_url": "https://qxt.example/send"})
    monkeypatch.setattr(parser, "get_access_token", AsyncMock(return_value="token"))
    session = FakeSession([FakeResponse(), FakeResponse()])
    monkeypatch.setattr(qxt_im_tool, "get_dispatch_session", lambda: session)
    await parser.post_message(payload={"content": content,
        "metadata": {"reply_target": {"type": "qxt", **recipient}},
        "attachments": [{"storage": "qxt", "frontend_id": "qxt", "media_id": "media-1"}]})
    assert len(session.calls) == (2 if content else 1)
    assert session.calls[-1]["json"] == {**recipient, "type": "file", "message": {"media_id": "media-1"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [{}, {"media_id": ""}, {"media_id": 123}])
async def test_qxt_upload_requires_a_valid_media_id(monkeypatch, reply):
    parser = im_tools.QxtIMParser(frontend_id="qxt", frontend_config={"send_msg_url": "https://qxt.example/send"})
    monkeypatch.setattr(parser, "get_access_token", AsyncMock(return_value="token"))
    session = FakeSession([FakeResponse(json_data=reply)])
    monkeypatch.setattr(qxt_im_tool, "get_dispatch_session", lambda: session)
    with pytest.raises(RuntimeError, match="missing media_id"):
        await parser.upload_outbound_attachment(data=b"pdf", filename="report.pdf", content_type="application/pdf")
    call = session.calls[0]
    assert call["url"] == "https://qxt.example/v2/media/upload"
    assert call["params"] == {"access_token": "token"}
    fields = call["data"]._fields
    assert fields[0][0]["name"] == "type" and fields[0][2] == "file"
    assert fields[1][0]["name"] == "media" and fields[1][0]["filename"] == "report.pdf"
    assert fields[1][2] == b"pdf"


@pytest.mark.asyncio
@pytest.mark.parametrize("filename,content_type,kind,key,file_type", [
    ("report.pdf", "application/pdf", "file", "file_key", "pdf"),
    ("photo.png", "image/png", "image", "image_key", None),
    ("voice.opus", "audio/ogg", "audio", "file_key", "opus"),
    ("video.mp4", "video/mp4", "video", "file_key", "mp4"),
])
async def test_feishu_upload_and_media_reply(monkeypatch, filename, content_type, kind, key, file_type):
    parser = CapturingFeishuParser()
    uploads = []
    def upload(request):
        body = request.request_body
        stream = body.image if kind == "image" else body.file
        uploads.append(stream.read())
        if file_type:
            assert body.file_type == file_type and body.file_name == filename
        else:
            assert body.image_type == "message"
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(**{key: "media-key"}))
    api = SimpleNamespace(create=upload)
    monkeypatch.setattr(parser, "_get_api_client", lambda: SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(image=api, file=api))))
    media = await parser.upload_outbound_attachment(data=b"file bytes", filename=filename, content_type=content_type)
    assert media == {"storage": "feishu", "frontend_id": "feishu", "upload_type": kind, key: "media-key"}
    await parser.post_message(payload={"content": "", "attachments": [media],
        "metadata": {"reply_target": {"type": "feishu", "message_id": "om_1", "receive_id": "oc_1"}}})
    assert uploads == [b"file bytes"]
    assert len(parser.sent) == 1 and parser.sent[0]["mode"] == "reply"
    assert parser.sent[0]["message_type"] == kind
    assert json.loads(parser.sent[0]["content"]) == {key: "media-key"}
