from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

from container_up.binding_repository import BindingRepository
from container_up.bucket_client import BucketClient
from container_up.bucket_manager import BucketManager
from container_up.workspace_manager import WorkspaceManager


@dataclass(frozen=True)
class UserInstanceRuntime:
    user_id: str
    workspace_path: str
    bucket_id: str
    bucket_url: str
    instance_id: str
    frontend_id: str | None = None
    app_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BucketScheduler:
    def __init__(
        self,
        *,
        repo: BindingRepository,
        workspace_manager: WorkspaceManager,
        bucket_manager: BucketManager,
        bucket_client: BucketClient,
    ) -> None:
        self._repo = repo
        self._workspace_manager = workspace_manager
        self._bucket_manager = bucket_manager
        self._bucket_client = bucket_client
        self._user_locks: dict[str, asyncio.Lock] = {}

    async def get_or_create_user_instance(
        self,
        *,
        user_id: str,
        frontend_id: str | None,
        app_id: str | None = None,
    ) -> UserInstanceRuntime:
        lock = self._user_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            existing = self._repo.get_user_instance(user_id)
            if existing is not None and existing.get("status") == "online" and existing.get("bucket_id"):
                bucket = self._repo.get_bucket(str(existing["bucket_id"]))
                if bucket is not None:
                    self._repo.touch_user_activity(user_id)
                    return self._runtime_from_records(existing, bucket)

            workspace = self._workspace_manager.get_or_create_workspace(user_id)
            user, bucket, created = self._repo.reserve_user_instance(
                user_id=user_id,
                workspace_path=str(workspace),
                frontend_id=frontend_id,
                app_id=app_id,
            )
            if not created:
                self._repo.touch_user_activity(user_id)
                return self._runtime_from_records(user, bucket)

            runtime = self._runtime_from_records(user, bucket)
            try:
                await self._bucket_manager.ensure_bucket_exists(bucket)
                await self._bucket_manager.wait_bucket_ready(bucket)
                await self._bucket_client.create_user_instance(
                    runtime.bucket_url,
                    {
                        "frontend_id": frontend_id or "",
                        "user_id": user_id,
                        "instance_id": runtime.instance_id,
                        "workspace_path": runtime.workspace_path,
                        "app_id": app_id or "",
                    },
                )
            except Exception:
                self._repo.rollback_user_instance_reservation(user_id, runtime.bucket_id)
                raise

            user = self._repo.mark_user_instance_online(user_id)
            bucket = self._repo.get_bucket(runtime.bucket_id)
            if bucket is None:
                raise RuntimeError(f"bucket disappeared after online transition: {runtime.bucket_id}")
            return self._runtime_from_records(user, bucket)

    async def route_inbound(
        self,
        *,
        frontend_id: str,
        user_id: str,
        payload: dict[str, Any],
    ) -> UserInstanceRuntime:
        runtime = await self.get_or_create_user_instance(
            user_id=user_id,
            frontend_id=frontend_id,
            app_id=str(payload.get("app_id") or ""),
        )
        packet = dict(payload)
        packet["frontend_id"] = frontend_id
        packet["user_id"] = user_id
        packet["instance_id"] = runtime.instance_id
        await self._bucket_client.forward_inbound(runtime.bucket_url, packet)
        self._repo.touch_user_activity(user_id)
        return runtime

    async def route_cancel(
        self,
        *,
        frontend_id: str,
        user_id: str,
        payload: dict[str, Any],
    ) -> UserInstanceRuntime | None:
        user = self._repo.get_user_instance(user_id)
        if user is None or user.get("status") != "online" or not user.get("bucket_id"):
            return None
        bucket = self._repo.get_bucket(str(user["bucket_id"]))
        if bucket is None:
            return None
        runtime = self._runtime_from_records(user, bucket)
        packet = dict(payload)
        packet["frontend_id"] = frontend_id
        packet["user_id"] = user_id
        packet["instance_id"] = runtime.instance_id
        await self._bucket_client.forward_cancel(runtime.bucket_url, packet)
        self._repo.touch_user_activity(user_id)
        return runtime

    async def release_user_instance(self, user_id: str) -> dict[str, Any] | None:
        user = self._repo.get_user_instance(user_id)
        if user is None or user.get("status") != "online" or not user.get("bucket_id"):
            return user
        bucket = self._repo.get_bucket(str(user["bucket_id"]))
        if bucket is not None and user.get("instance_id"):
            try:
                await self._bucket_client.destroy_user_instance(
                    str(bucket["service_host"]),
                    str(user["instance_id"]),
                )
            finally:
                return self._repo.release_user_instance(
                    user_id,
                    bucket_id=str(user["bucket_id"]),
                    instance_id=str(user["instance_id"]),
                )
        return self._repo.release_user_instance(user_id)

    def sync_runtime_release(
        self,
        *,
        user_id: str,
        bucket_id: str | None,
        instance_id: str | None,
    ) -> dict[str, Any] | None:
        return self._repo.release_user_instance(
            user_id,
            bucket_id=bucket_id,
            instance_id=instance_id,
        )

    @staticmethod
    def _runtime_from_records(user: dict[str, Any], bucket: dict[str, Any]) -> UserInstanceRuntime:
        return UserInstanceRuntime(
            user_id=str(user["user_id"]),
            workspace_path=str(user["workspace_path"]),
            bucket_id=str(bucket["bucket_id"]),
            bucket_url=str(bucket["service_host"]).rstrip("/"),
            instance_id=str(user["instance_id"] or user["user_id"]),
            frontend_id=str(user.get("frontend_id") or "") or None,
            app_id=str(user.get("app_id") or "") or None,
        )
