from __future__ import annotations

import json
from unittest.mock import AsyncMock
from typing import Any

import pytest

from container_up import feishu_im_tool
from container_up import qxt_im_tool as im_tools
from container_up import qxt_im_tool


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
    ("event_type", "chat_type", "recipient"),
    [
        ("p2p_chat_receive_msg", "single", {"to_single_uid": "user-1"}),
        ("group_chat_at_bot", "group", {"to_group_id": "chat-1"}),
    ],
)
def test_qxt_subscribe_payload_is_normalized_to_unified_im_event(
    event_type: str, chat_type: str, recipient: dict[str, str],
) -> None:
    parser = im_tools.QxtIMParser(frontend_id="qxt", frontend_config={})
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
            "user_id": "user-1",
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
