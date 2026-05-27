from __future__ import annotations

import httpx
import pytest

from container_up.bucket_client import BucketClient


@pytest.mark.asyncio
async def test_bucket_client_post_surfaces_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("POST", "http://bucket-1/instances")
    response = httpx.Response(503, request=request, text='{"detail":"frontend not configured"}')

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]) -> httpx.Response:
            return response

    monkeypatch.setattr("container_up.bucket_client.httpx.AsyncClient", _FakeAsyncClient)

    client = BucketClient()

    with pytest.raises(RuntimeError, match="frontend not configured"):
        await client.create_user_instance(
            "http://bucket-1",
            {
                "frontend_id": "web-maim",
                "user_id": "user-1",
                "instance_id": "inst-1",
                "workspace_path": "/tmp/ws/user-1",
            },
        )
