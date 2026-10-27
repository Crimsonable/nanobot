from __future__ import annotations

import pytest

from channel_gateway import http_state
from channel_gateway.web_im_tool import WebIMParser


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
    def __init__(self, responses: list[_FakeResponse] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.responses = list(responses or [_FakeResponse()])

    def post(self, url: str, *, json: dict[str, object]):
        self.calls.append((url, json))
        return _FakeRequestContext(self.responses.pop(0))


@pytest.mark.asyncio
async def test_web_im_parser_posts_normalized_outbound_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_session = _FakeSession()
    monkeypatch.setattr(http_state, "_dispatch_session", fake_session)

    parser = WebIMParser(
        frontend_id="web-main",
        frontend_config={"send_msg_url": "http://web-server:8090/outbound"},
    )

    result = await parser.post_message(
        payload={
            "chat_id": "chat-1",
            "content": "hello",
            "attachments": [{"url": "https://files.example.com/demo.png"}],
            "metadata": {"user_id": "user-1", "frontend_id": "web-main"},
        }
    )

    assert result == {"ok": True}
    assert fake_session.calls == [
        (
            "http://web-server:8090/outbound",
            {
                "frontend_id": "web-main",
                "user_id": "user-1",
                "chat_id": "chat-1",
                "content": "hello",
                "attachments": [{"url": "https://files.example.com/demo.png"}],
                "metadata": {"user_id": "user-1", "frontend_id": "web-main"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_web_im_parser_requires_send_url() -> None:
    parser = WebIMParser(frontend_id="web-main", frontend_config={})
    with pytest.raises(RuntimeError, match="send_msg_url"):
        await parser.post_message(payload={"content": "hello"})


@pytest.mark.asyncio
async def test_web_im_parser_does_not_repeat_uncertain_delivery(monkeypatch) -> None:
    fake_session = _FakeSession(responses=[_FakeResponse(status=503, text="unavailable")])
    monkeypatch.setattr(http_state, "_dispatch_session", fake_session)
    parser = WebIMParser(frontend_id="web-main",
        frontend_config={"send_msg_url": "http://web-server:8090/outbound"})
    with pytest.raises(RuntimeError, match="503"):
        await parser.post_message(payload={"chat_id": "chat", "content": "hello",
            "metadata": {"user_id": "alice", "frontend_id": "web-main"}})
    assert len(fake_session.calls) == 1
