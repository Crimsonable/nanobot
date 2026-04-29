from __future__ import annotations

from pathlib import Path

import pytest

from container_up import http_state
from container_up.web_im_tool import WebIMParser


class _FakeResponse:
    def __init__(self, *, status: int = 200, text: str = '{"ok": true}') -> None:
        self.status = status
        self._text = text
        self.headers = {"Content-Type": "application/json"}

    async def text(self) -> str:
        return self._text


class _FakeRequestContext:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post(self, url: str, *, json: dict[str, object]):
        self.calls.append((url, json))
        return _FakeRequestContext(_FakeResponse())


@pytest.mark.asyncio
async def test_web_im_parser_posts_normalized_outbound_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_session = _FakeSession()
    monkeypatch.setattr(http_state, "_dispatch_session", fake_session)
    monkeypatch.setattr(
        "container_up.web_im_tool.normalize_outbound_attachments",
        lambda attachments, frontend_id=None: ["/abs/demo.png"],
    )
    parser = WebIMParser(
        frontend_id="web-main",
        frontend_config={"send_msg_url": "http://web-server.nanobot:8090/outbound"},
    )

    result = await parser.post_message_with_retry(
        payload={
            "chat_id": "chat-1",
            "content": "hello",
            "attachments": ["cache/demo.png"],
            "metadata": {"usr_id": "user-1", "frontend_id": "web-main"},
        }
    )

    assert result == {"ok": True}
    assert fake_session.calls == [
        (
            "http://web-server.nanobot:8090/outbound",
            {
                "frontend_id": "web-main",
                "user_id": "user-1",
                "chat_id": "chat-1",
                "content": "hello",
                "attachments": ["/abs/demo.png"],
                "metadata": {"usr_id": "user-1", "frontend_id": "web-main"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_web_im_parser_requires_send_url() -> None:
    parser = WebIMParser(frontend_id="web-main")
    with pytest.raises(RuntimeError, match="send_msg_url"):
        await parser.post_message_with_retry(payload={"content": "hello"})
