from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from web_server.app import app


def test_web_server_inbound_forwards_to_container_up(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"status": "accepted", "instance_id": "u-1"}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]) -> _FakeResponse:
            captured["url"] = url
            captured["json"] = json
            return _FakeResponse()

    monkeypatch.setattr("web_server.app.CONTAINER_UP_BASE_URL", "http://container-up.nanobot:8080")
    monkeypatch.setattr("web_server.app.httpx.AsyncClient", _FakeAsyncClient)

    with TestClient(app) as client:
        response = client.post(
            "/inbound",
            json={
                "frontend_id": "web-main",
                "user_id": "u-1",
                "chat_id": "chat-1",
                "content": "hello",
                "attachments": [],
                "metadata": {"k": "v"},
                "raw": {"source": "web"},
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "instance_id": "u-1"}
    assert captured["url"] == "http://container-up.nanobot:8080/inbound/web-main"
    assert captured["json"] == {
        "user_id": "u-1",
        "chat_id": "chat-1",
        "content": "hello",
        "attachments": [],
        "metadata": {"k": "v"},
        "raw": {"source": "web"},
    }


def test_web_server_inbound_propagates_http_error(monkeypatch) -> None:
    request = httpx.Request("POST", "http://container-up.nanobot:8080/inbound/web-main")
    response = httpx.Response(503, request=request, text="backend unavailable")

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]):
            raise httpx.HTTPStatusError("failed", request=request, response=response)

    monkeypatch.setattr("web_server.app.CONTAINER_UP_BASE_URL", "http://container-up.nanobot:8080")
    monkeypatch.setattr("web_server.app.httpx.AsyncClient", _FakeAsyncClient)

    with TestClient(app) as client:
        result = client.post(
            "/inbound",
            json={"frontend_id": "web-main", "user_id": "u-1", "content": "hello"},
        )

    assert result.status_code == 503
    assert result.json() == {"detail": "backend unavailable"}
