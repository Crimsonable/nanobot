from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from container_up.bucket_scheduler import BucketScheduler, UserInstanceRuntime


class _FakeRepo:
    def __init__(self) -> None:
        self.touched: list[str] = []

    def touch_user_activity(self, user_id: str) -> None:
        self.touched.append(user_id)


@pytest.mark.asyncio
async def test_route_inbound_preserves_request_id_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _FakeRepo()
    bucket_client = AsyncMock()
    scheduler = BucketScheduler(
        repo=repo,
        workspace_manager=object(),
        bucket_manager=object(),
        bucket_client=bucket_client,
    )
    runtime = UserInstanceRuntime(
        user_id="user-1",
        workspace_path="/tmp/ws/user-1",
        bucket_id="bucket-0",
        bucket_url="http://bucket-0",
        instance_id="inst-1",
        frontend_id="feishu-main",
    )
    monkeypatch.setattr(
        scheduler,
        "get_or_create_user_instance",
        AsyncMock(return_value=runtime),
    )

    payload = {
        "chat_id": "conv-1",
        "content": "hello",
        "attachments": ["/tmp/a.png"],
        "metadata": {"trace_id": "trace-1"},
        "raw": {"source": "im"},
    }
    returned = await scheduler.route_inbound(
        frontend_id="feishu-main",
        user_id="user-1",
        payload=payload,
    )

    bucket_client.forward_inbound.assert_awaited_once_with(
        "http://bucket-0",
        {
            "chat_id": "conv-1",
            "content": "hello",
            "attachments": ["/tmp/a.png"],
            "metadata": {"trace_id": "trace-1"},
            "raw": {"source": "im"},
            "frontend_id": "feishu-main",
            "user_id": "user-1",
            "instance_id": "inst-1",
        },
    )
    assert returned == runtime
    assert repo.touched == ["user-1"]


@pytest.mark.asyncio
async def test_route_cancel_preserves_request_id_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = UserInstanceRuntime(
        user_id="user-1",
        workspace_path="/tmp/ws/user-1",
        bucket_id="bucket-0",
        bucket_url="http://bucket-0",
        instance_id="inst-1",
        frontend_id="feishu-main",
    )

    class _CancelRepo(_FakeRepo):
        def get_user_instance(self, user_id: str) -> dict[str, str]:
            assert user_id == "user-1"
            return {
                "status": "online",
                "bucket_id": "bucket-0",
            }

        def get_bucket(self, bucket_id: str) -> dict[str, str]:
            assert bucket_id == "bucket-0"
            return {"bucket_id": "bucket-0", "service_host": "http://bucket-0"}

    repo = _CancelRepo()
    bucket_client = AsyncMock()
    scheduler = BucketScheduler(
        repo=repo,
        workspace_manager=object(),
        bucket_manager=object(),
        bucket_client=bucket_client,
    )
    monkeypatch.setattr(
        scheduler,
        "_runtime_from_records",
        lambda _user, _bucket: runtime,
    )

    returned = await scheduler.route_cancel(
        frontend_id="feishu-main",
        user_id="user-1",
        payload={
            "chat_id": "conv-1",
            "metadata": {"trace_id": "trace-1"},
            "raw": {"source": "im"},
        },
    )

    bucket_client.forward_cancel.assert_awaited_once_with(
        "http://bucket-0",
        {
            "chat_id": "conv-1",
            "metadata": {"trace_id": "trace-1"},
            "raw": {"source": "im"},
            "frontend_id": "feishu-main",
            "user_id": "user-1",
            "instance_id": "inst-1",
        },
    )
    assert returned == runtime
    assert repo.touched == ["user-1"]
