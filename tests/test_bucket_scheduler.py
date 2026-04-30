from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
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
async def test_get_or_create_reuses_live_online_instance() -> None:
    class _Repo(_FakeRepo):
        def get_user_instance(self, user_id: str) -> dict[str, str]:
            assert user_id == "user-1"
            return {
                "user_id": "user-1",
                "workspace_path": "/tmp/ws/user-1",
                "status": "online",
                "bucket_id": "bucket-0",
                "instance_id": "inst-1",
                "frontend_id": "feishu-main",
            }

        def get_bucket(self, bucket_id: str) -> dict[str, str]:
            assert bucket_id == "bucket-0"
            return {
                "bucket_id": "bucket-0",
                "service_host": "http://bucket-0",
            }

    repo = _Repo()
    bucket_client = AsyncMock()
    scheduler = BucketScheduler(
        repo=repo,
        workspace_manager=AsyncMock(),
        bucket_manager=AsyncMock(),
        bucket_client=bucket_client,
    )

    runtime = await scheduler.get_or_create_user_instance(
        user_id="user-1",
        frontend_id="feishu-main",
    )

    bucket_client.get_user_instance.assert_awaited_once_with("http://bucket-0", "inst-1")
    assert runtime == UserInstanceRuntime(
        user_id="user-1",
        workspace_path="/tmp/ws/user-1",
        bucket_id="bucket-0",
        bucket_url="http://bucket-0",
        instance_id="inst-1",
        frontend_id="feishu-main",
    )
    assert repo.touched == ["user-1"]


@pytest.mark.asyncio
async def test_get_or_create_new_user() -> None:
    class _Repo(_FakeRepo):
        def reserve_user_instance(
            self,
            *,
            user_id: str,
            workspace_path: str,
            frontend_id: str | None,
        ) -> tuple[dict[str, str], dict[str, str], bool]:
            assert user_id == "user-1"
            assert workspace_path == "/tmp/ws/user-1"
            assert frontend_id == "web-main"
            return (
                {
                    "user_id": "user-1",
                    "workspace_path": "/tmp/ws/user-1",
                    "status": "creating",
                    "bucket_id": "bucket-0",
                    "instance_id": "inst-1",
                    "frontend_id": "web-main",
                },
                {
                    "bucket_id": "bucket-0",
                    "service_host": "http://bucket-0",
                    "bucket_name": "nanobot-bucket-0",
                    "namespace": "nanobot",
                },
                True,
            )

        def mark_user_instance_online(self, user_id: str) -> dict[str, str]:
            assert user_id == "user-1"
            return {
                "user_id": "user-1",
                "workspace_path": "/tmp/ws/user-1",
                "status": "online",
                "bucket_id": "bucket-0",
                "instance_id": "inst-1",
                "frontend_id": "web-main",
            }

        def get_bucket(self, bucket_id: str) -> dict[str, str]:
            assert bucket_id == "bucket-0"
            return {
                "bucket_id": "bucket-0",
                "service_host": "http://bucket-0",
            }

    class _WorkspaceManager:
        def get_or_create_workspace(self, frontend_id: str, user_id: str) -> str:
            assert frontend_id == "web-main"
            assert user_id == "user-1"
            return "/tmp/ws/user-1"

    repo = _Repo()
    bucket_manager = AsyncMock()
    bucket_client = AsyncMock()
    scheduler = BucketScheduler(
        repo=repo,
        workspace_manager=_WorkspaceManager(),
        bucket_manager=bucket_manager,
        bucket_client=bucket_client,
    )

    runtime = await scheduler.get_or_create_user_instance(
        user_id="user-1",
        frontend_id="web-main",
    )

    bucket_manager.ensure_bucket_exists.assert_awaited_once()
    bucket_manager.wait_bucket_ready.assert_awaited_once()
    bucket_client.create_user_instance.assert_awaited_once_with(
        "http://bucket-0",
        {
            "frontend_id": "web-main",
            "user_id": "user-1",
            "instance_id": "inst-1",
            "workspace_path": "/tmp/ws/user-1",
        },
    )
    assert runtime.frontend_id == "web-main"


@pytest.mark.asyncio
async def test_get_or_create_recreates_stale_online_instance() -> None:
    request = httpx.Request("GET", "http://bucket-0/instances/inst-1")
    response = httpx.Response(404, request=request)

    class _Repo(_FakeRepo):
        def __init__(self) -> None:
            super().__init__()
            self.released: list[tuple[str, str | None, str | None]] = []

        def get_user_instance(self, user_id: str) -> dict[str, str]:
            assert user_id == "user-1"
            return {
                "user_id": "user-1",
                "workspace_path": "/tmp/ws/user-1",
                "status": "online",
                "bucket_id": "bucket-0",
                "instance_id": "inst-1",
                "frontend_id": "feishu-main",
            }

        def get_bucket(self, bucket_id: str) -> dict[str, str]:
            if bucket_id == "bucket-0":
                return {
                    "bucket_id": "bucket-0",
                    "service_host": "http://bucket-0",
                }
            assert bucket_id == "bucket-1"
            return {
                "bucket_id": "bucket-1",
                "service_host": "http://bucket-1",
            }

        def release_user_instance(
            self,
            user_id: str,
            *,
            bucket_id: str | None = None,
            instance_id: str | None = None,
        ) -> dict[str, str]:
            self.released.append((user_id, bucket_id, instance_id))
            return {"status": "destroyed"}

        def reserve_user_instance(
            self,
            *,
            user_id: str,
            workspace_path: str,
            frontend_id: str | None,
        ) -> tuple[dict[str, str], dict[str, str], bool]:
            assert user_id == "user-1"
            assert workspace_path == "/tmp/ws/user-1"
            assert frontend_id == "feishu-main"
            return (
                {
                    "user_id": "user-1",
                    "workspace_path": "/tmp/ws/user-1",
                    "status": "creating",
                    "bucket_id": "bucket-1",
                    "instance_id": "inst-1",
                    "frontend_id": "feishu-main",
                },
                {
                    "bucket_id": "bucket-1",
                    "service_host": "http://bucket-1",
                    "bucket_name": "nanobot-bucket-1",
                    "namespace": "nanobot",
                },
                True,
            )

        def mark_user_instance_online(self, user_id: str) -> dict[str, str]:
            assert user_id == "user-1"
            return {
                "user_id": "user-1",
                "workspace_path": "/tmp/ws/user-1",
                "status": "online",
                "bucket_id": "bucket-1",
                "instance_id": "inst-1",
                "frontend_id": "feishu-main",
            }

    repo = _Repo()

    class _WorkspaceManager:
        def get_or_create_workspace(self, frontend_id: str, user_id: str) -> str:
            assert frontend_id == "feishu-main"
            assert user_id == "user-1"
            return "/tmp/ws/user-1"

    bucket_manager = AsyncMock()
    bucket_client = AsyncMock()
    bucket_client.get_user_instance.side_effect = httpx.HTTPStatusError(
        "not found",
        request=request,
        response=response,
    )
    scheduler = BucketScheduler(
        repo=repo,
        workspace_manager=_WorkspaceManager(),
        bucket_manager=bucket_manager,
        bucket_client=bucket_client,
    )

    runtime = await scheduler.get_or_create_user_instance(
        user_id="user-1",
        frontend_id="feishu-main",
    )

    assert repo.released == [("user-1", "bucket-0", "inst-1")]
    bucket_manager.ensure_bucket_exists.assert_awaited_once()
    bucket_manager.wait_bucket_ready.assert_awaited_once()
    bucket_client.create_user_instance.assert_awaited_once_with(
        "http://bucket-1",
        {
            "frontend_id": "feishu-main",
            "user_id": "user-1",
            "instance_id": "inst-1",
            "workspace_path": "/tmp/ws/user-1",
        },
    )
    assert runtime.bucket_id == "bucket-1"


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
