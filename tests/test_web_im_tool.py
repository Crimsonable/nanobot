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

    async def fake_prepare(
        attachments,
        *,
        frontend_id,
        user_id,
        frontend_config=None,
    ):
        return [{"url": "https://files.example.com/demo.png"}]

    monkeypatch.setattr(
        "container_up.web_im_tool.prepare_outbound_attachments",
        fake_prepare,
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
                "attachments": [{"url": "https://files.example.com/demo.png"}],
                "metadata": {"usr_id": "user-1", "frontend_id": "web-main"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_web_im_parser_requires_send_url() -> None:
    parser = WebIMParser(frontend_id="web-main")
    with pytest.raises(RuntimeError, match="send_msg_url"):
        await parser.post_message_with_retry(payload={"content": "hello"})


@pytest.mark.asyncio
async def test_web_im_parser_retries_transient_server_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_session = _FakeSession(
        responses=[
            _FakeResponse(status=503, text="temporarily unavailable"),
            _FakeResponse(status=200, text='{"ok": true}'),
        ]
    )
    monkeypatch.setattr(http_state, "_dispatch_session", fake_session)

    async def fake_prepare(
        attachments,
        *,
        frontend_id,
        user_id,
        frontend_config=None,
    ):
        return []

    async def fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(
        "container_up.web_im_tool.prepare_outbound_attachments",
        fake_prepare,
    )
    monkeypatch.setattr("container_up.web_im_tool.asyncio.sleep", fake_sleep)

    parser = WebIMParser(
        frontend_id="web-main",
        frontend_config={
            "send_msg_url": "http://web-server.nanobot:8090/outbound",
            "send_msg_retry_count": 2,
            "send_msg_retry_backoff": 0.01,
        },
    )

    result = await parser.post_message_with_retry(
        payload={
            "chat_id": "chat-1",
            "content": "hello",
            "attachments": [],
            "metadata": {"usr_id": "user-1", "frontend_id": "web-main"},
        }
    )

    assert result == {"ok": True}
    assert len(fake_session.calls) == 2
