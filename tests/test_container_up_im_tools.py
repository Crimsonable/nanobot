from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from container_up import im_tools


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

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_post_message_with_attachment_uploads_then_sends_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = tmp_path / "report.txt"
    attachment.write_text("hello", encoding="utf-8")
    session = FakeSession(
        [
            FakeResponse(status=200, text='{"ok":true}'),
            FakeResponse(status=200, text='{"media_id":"mid-1"}', json_data={"media_id": "mid-1"}),
            FakeResponse(status=200, text='{"ok":true}'),
        ]
    )
    parser = im_tools.QxtIMParser()

    monkeypatch.setattr(im_tools, "SEND_MSG_URL", "https://im.example.com/v2/message/bot_send_to_conversation")
    monkeypatch.setattr(im_tools, "get_dispatch_session", lambda: session)
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
    assert session.calls[1]["url"] == "https://im.example.com/v2/media/upload"
    assert session.calls[1]["params"] == {"access_token": "token-1"}
    assert session.calls[2]["url"] == "https://im.example.com/v2/message/bot_send_to_conversation"
    assert session.calls[2]["json"] == {
        "to_single_uid": "user-1",
        "type": "file",
        "message": {"media_id": "mid-1"},
    }


@pytest.mark.asyncio
async def test_post_message_skips_empty_text_and_sends_attachments_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = tmp_path / "image.png"
    attachment.write_bytes(b"png")
    session = FakeSession(
        [
            FakeResponse(status=200, text='{"media_id":"mid-2"}', json_data={"media_id": "mid-2"}),
            FakeResponse(status=200, text='{"ok":true}'),
        ]
    )
    parser = im_tools.QxtIMParser()

    monkeypatch.setattr(im_tools, "SEND_MSG_URL", "https://im.example.com/v2/message/bot_send_to_conversation")
    monkeypatch.setattr(im_tools, "get_dispatch_session", lambda: session)
    monkeypatch.setattr(parser, "get_access_token", lambda: "token-2")

    result = await parser.post_message_with_retry(
        payload={
            "to_single_uid": "user-2",
            "type": "text",
            "message": {"content": ""},
            "attachments": [str(attachment)],
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
