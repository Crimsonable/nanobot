from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

import httpx
from loguru import logger

from container_up.binding_repository import BindingRepository
from container_up.binding_repository import build_instance_id
from container_up.bucket_client import BucketClient
from container_up.bucket_manager import BucketManager
from container_up.workspace_manager import WorkspaceManager

_BUCKET_CAPACITY_ERROR = "bucket has reached max process capacity"


@dataclass(frozen=True)
class UserInstanceRuntime:
    user_id: str
    workspace_path: str
    bucket_id: str
    bucket_url: str
    instance_id: str
    frontend_id: str | None = None

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
        self._pending_user_touches: set[str] = set()
        self._touch_tasks: set[asyncio.Task[None]] = set()

    async def get_or_create_user_instance(
        self,
        *,
        user_id: str,
        frontend_id: str | None,
    ) -> UserInstanceRuntime:
        if not frontend_id:
            raise RuntimeError("frontend_id is required to allocate a workspace")
        user_key = f"{frontend_id}:{user_id}"
        lock = self._user_locks.setdefault(user_key, asyncio.Lock())
        async with lock:
            existing = self._repo.get_user_instance(frontend_id, user_id)
            if existing is not None and existing.get("status") == "online" and existing.get("bucket_id"):
                runtime = await self._get_live_runtime(existing)
                if runtime is not None:
                    self._schedule_touch_user_activity(frontend_id, user_id)
                    return runtime

            workspace = self._workspace_manager.get_or_create_workspace(frontend_id, user_id)
            for attempt in range(3):
                user, bucket, created = self._repo.reserve_user_instance(
                    frontend_id=frontend_id,
                    user_id=user_id,
                    workspace_path=str(workspace),
                )
                self._assert_binding_consistency(
                    user,
                    frontend_id=frontend_id,
                    user_id=user_id,
                    workspace_path=str(workspace),
                )
                if not created:
                    self._schedule_touch_user_activity(frontend_id, user_id)
                    return self._runtime_from_records(user, bucket)

                runtime = self._runtime_from_records(user, bucket)
                try:
                    await self._bucket_manager.ensure_bucket_exists(bucket)
                    await self._bucket_manager.wait_bucket_ready(bucket)
                    await self._bucket_client.create_user_instance(
                        runtime.bucket_url,
                        {
                            "frontend_id": frontend_id,
                            "user_id": user_id,
                            "instance_id": runtime.instance_id,
                            "workspace_path": runtime.workspace_path,
                        },
                    )
                except Exception as exc:
                    self._repo.rollback_user_instance_reservation(
                        frontend_id,
                        user_id,
                        runtime.bucket_id,
                    )
                    if self._is_bucket_capacity_error(exc) and attempt < 2:
                        logger.warning(
                            "bucket reported capacity exhaustion despite scheduler reservation; "
                            "marking bucket full and retrying frontend_id={} user_id={} bucket_id={}",
                            frontend_id,
                            user_id,
                            runtime.bucket_id,
                        )
                        self._mark_bucket_full(runtime.bucket_id)
                        continue
                    raise

                user = self._repo.mark_user_instance_online(frontend_id, user_id)
                self._assert_binding_consistency(
                    user,
                    frontend_id=frontend_id,
                    user_id=user_id,
                    workspace_path=str(workspace),
                )
                bucket = self._repo.get_bucket(runtime.bucket_id)
                if bucket is None:
                    raise RuntimeError(f"bucket disappeared after online transition: {runtime.bucket_id}")
                return self._runtime_from_records(user, bucket)

            raise RuntimeError(
                f"failed to allocate runtime after retrying bucket capacity for {frontend_id}/{user_id}"
            )

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
        )
        packet = dict(payload)
        packet["frontend_id"] = frontend_id
        packet["user_id"] = user_id
        packet["instance_id"] = runtime.instance_id
        await self._bucket_client.forward_inbound(runtime.bucket_url, packet)
        self._schedule_touch_user_activity(frontend_id, user_id)
        return runtime

    async def route_cancel(
        self,
        *,
        frontend_id: str,
        user_id: str,
        payload: dict[str, Any],
    ) -> UserInstanceRuntime | None:
        user = self._repo.get_user_instance(frontend_id, user_id)
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
        self._schedule_touch_user_activity(frontend_id, user_id)
        return runtime

    def _schedule_touch_user_activity(self, frontend_id: str, user_id: str) -> None:
        user_key = f"{frontend_id}:{user_id}"
        if user_key in self._pending_user_touches:
            return
        self._pending_user_touches.add(user_key)
        task = asyncio.create_task(self._run_touch_user_activity(user_key, frontend_id, user_id))
        self._touch_tasks.add(task)
        task.add_done_callback(self._on_touch_task_done)

    async def _run_touch_user_activity(
        self,
        user_key: str,
        frontend_id: str,
        user_id: str,
    ) -> None:
        try:
            await asyncio.to_thread(self._repo.touch_user_activity, frontend_id, user_id)
        finally:
            self._pending_user_touches.discard(user_key)

    def _on_touch_task_done(self, task: asyncio.Task[None]) -> None:
        self._touch_tasks.discard(task)
        try:
            task.result()
        except Exception as exc:  # pragma: no cover
            logger.warning("touch_user_activity background task failed: {}", exc)

    async def shutdown(self) -> None:
        if not self._touch_tasks:
            return
        tasks = list(self._touch_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._touch_tasks.clear()
        self._pending_user_touches.clear()

    async def release_user_instance(self, frontend_id: str, user_id: str) -> dict[str, Any] | None:
        user = self._repo.get_user_instance(frontend_id, user_id)
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
                    frontend_id,
                    user_id,
                    bucket_id=str(user["bucket_id"]),
                    instance_id=str(user["instance_id"]),
                )
        return self._repo.release_user_instance(frontend_id, user_id)

    def sync_runtime_release(
        self,
        *,
        user_id: str,
        frontend_id: str | None,
        bucket_id: str | None,
        instance_id: str | None,
    ) -> dict[str, Any] | None:
        if frontend_id:
            return self._repo.release_user_instance(
                frontend_id,
                user_id,
                bucket_id=bucket_id,
                instance_id=instance_id,
            )
        if instance_id:
            user = self._repo.get_user_instance_by_instance_id(instance_id)
            if user is not None:
                return self._repo.release_user_instance(
                    str(user["frontend_id"]),
                    str(user["user_id"]),
                    bucket_id=bucket_id,
                    instance_id=instance_id,
                )
        matches = self._repo.list_for_user(user_id)
        if len(matches) != 1:
            return None
        return self._repo.release_user_instance(
            str(matches[0]["frontend_id"]),
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
            instance_id=str(user.get("instance_id") or "")
            or build_instance_id(str(user.get("frontend_id") or ""), str(user["user_id"])),
            frontend_id=str(user.get("frontend_id") or "") or None,
        )

    @staticmethod
    def _assert_binding_consistency(
        user: dict[str, Any],
        *,
        frontend_id: str,
        user_id: str,
        workspace_path: str,
    ) -> None:
        actual_frontend = str(user.get("frontend_id") or "")
        actual_user = str(user.get("user_id") or "")
        actual_workspace = str(user.get("workspace_path") or "")
        actual_instance_id = str(user.get("instance_id") or "")
        expected_instance_id = build_instance_id(frontend_id, user_id)
        if actual_frontend != frontend_id:
            raise RuntimeError(
                f"binding frontend_id mismatch: expected {frontend_id}, got {actual_frontend}"
            )
        if actual_user != user_id:
            raise RuntimeError(
                f"binding user_id mismatch: expected {user_id}, got {actual_user}"
            )
        if actual_workspace != workspace_path:
            raise RuntimeError(
                "binding workspace_path mismatch: "
                f"expected {workspace_path}, got {actual_workspace}"
            )
        if actual_instance_id and actual_instance_id != expected_instance_id:
            raise RuntimeError(
                "binding instance_id mismatch: "
                f"expected {expected_instance_id}, got {actual_instance_id}"
            )

    @staticmethod
    def _is_bucket_capacity_error(exc: Exception) -> bool:
        return _BUCKET_CAPACITY_ERROR in str(exc)

    def _mark_bucket_full(self, bucket_id: str) -> None:
        touch_bucket = getattr(self._repo, "touch_bucket", None)
        if callable(touch_bucket):
            touch_bucket(bucket_id, status="full")

    async def _get_live_runtime(self, user: dict[str, Any]) -> UserInstanceRuntime | None:
        bucket_id = str(user.get("bucket_id") or "")
        if not bucket_id:
            return None
        bucket = self._repo.get_bucket(bucket_id)
        if bucket is None:
            self._repo.release_user_instance(
                str(user["frontend_id"]),
                str(user["user_id"]),
                bucket_id=bucket_id,
                instance_id=str(user.get("instance_id") or "") or None,
            )
            return None

        runtime = self._runtime_from_records(user, bucket)
        try:
            await self._bucket_client.get_user_instance(runtime.bucket_url, runtime.instance_id)
            return runtime
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "stale runtime binding for user_id={} bucket_id={} instance_id={} status={}; recreating",
                runtime.user_id,
                runtime.bucket_id,
                runtime.instance_id,
                exc.response.status_code,
            )
        except httpx.RequestError:
            logger.warning(
                "bucket probe failed for user_id={} bucket_id={} instance_id={}; recreating",
                runtime.user_id,
                runtime.bucket_id,
                runtime.instance_id,
            )

        self._repo.release_user_instance(
            runtime.frontend_id or "",
            runtime.user_id,
            bucket_id=runtime.bucket_id,
            instance_id=runtime.instance_id,
        )
        return None
