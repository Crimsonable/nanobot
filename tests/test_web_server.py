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


def test_web_server_create_instances_for_test(monkeypatch) -> None:
    captured_posts: list[dict[str, object]] = []

    class _FakeResponse:
        status_code = 200

        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]) -> _FakeResponse:
            captured_posts.append({"url": url, "json": json})
            return _FakeResponse({"status": "accepted", "instance_id": json["user_id"]})

    monkeypatch.setattr("web_server.app.CONTAINER_UP_BASE_URL", "http://container-up.nanobot:8080")
    monkeypatch.setattr("web_server.app.httpx.AsyncClient", _FakeAsyncClient)

    with TestClient(app) as client:
        response = client.post(
            "/test/create-instances",
            json={"frontend_id": "web-main", "n": 3, "content": "create test instance"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["frontend_id"] == "web-main"
    assert body["count"] == 3
    assert body["success_count"] == 3
    assert body["failure_count"] == 0
    assert len(body["requests"]) == 3
    assert len(body["responses"]) == 3
    assert len(captured_posts) == 3
    assert {item["url"] for item in captured_posts} == {
        "http://container-up.nanobot:8080/inbound/web-main"
    }

    expected_user_ids = [item["user_id"] for item in body["requests"]]
    expected_chat_ids = [item["chat_id"] for item in body["requests"]]
    assert all(user_id.startswith("test-user-") for user_id in expected_user_ids)
    assert all(chat_id.startswith("test-chat-") for chat_id in expected_chat_ids)

    forwarded_payloads = [item["json"] for item in captured_posts]
    assert [item["user_id"] for item in forwarded_payloads] == expected_user_ids
    assert [item["chat_id"] for item in forwarded_payloads] == expected_chat_ids
    assert all(item["content"] == "create test instance" for item in forwarded_payloads)
    assert all(item["status"] == "accepted" for item in body["responses"])
    assert [item["user_id"] for item in body["responses"]] == expected_user_ids
    assert [item["chat_id"] for item in body["responses"]] == expected_chat_ids


def test_web_server_create_instances_for_test_partial_failure(monkeypatch) -> None:
    captured_posts: list[dict[str, object]] = []

    class _FakeResponse:
        status_code = 200

        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]):
            captured_posts.append({"url": url, "json": json})
            user_id = str(json["user_id"])
            if user_id.endswith("-2"):
                raise httpx.HTTPStatusError(
                    "failed",
                    request=httpx.Request("POST", url),
                    response=httpx.Response(503, request=httpx.Request("POST", url), text="bucket busy"),
                )
            return _FakeResponse({"status": "accepted", "instance_id": user_id})

    monkeypatch.setattr("web_server.app.CONTAINER_UP_BASE_URL", "http://container-up.nanobot:8080")
    monkeypatch.setattr("web_server.app.httpx.AsyncClient", _FakeAsyncClient)

    with TestClient(app) as client:
        response = client.post(
            "/test/create-instances",
            json={"frontend_id": "web-main", "n": 3, "content": "create test instance"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial_success"
    assert body["success_count"] == 2
    assert body["failure_count"] == 1
    assert len(body["responses"]) == 3
    failed = [item for item in body["responses"] if item["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["error"] == "bucket busy"
