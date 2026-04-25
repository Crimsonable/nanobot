from __future__ import annotations

from typing import Any

from agent_gateway.clients.bucket_client import BucketClient
from agent_gateway.repositories.binding_repository import BindingRepository
from agent_gateway.services.bucket_allocator import BucketAllocator


class GatewayRouter:
    def __init__(
        self,
        repo: BindingRepository,
        allocator: BucketAllocator,
        bucket_client: BucketClient,
    ) -> None:
        self._repo = repo
        self._allocator = allocator
        self._bucket_client = bucket_client

    async def ensure_binding(self, frontend_id: str, user_id: str) -> dict[str, Any]:
        binding = self._repo.get(frontend_id, user_id)
        if binding is not None:
            return binding
        bucket_id = self._allocator.allocate()
        return self._repo.upsert(frontend_id, user_id, bucket_id)

    async def route_inbound(
        self,
        frontend_id: str,
        user_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        binding = await self.ensure_binding(frontend_id, user_id)
        await self._bucket_client.forward_inbound(binding["bucket_id"], payload)
        return binding

    async def route_cancel(
        self,
        frontend_id: str,
        user_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        binding = await self.ensure_binding(frontend_id, user_id)
        await self._bucket_client.forward_cancel(binding["bucket_id"], payload)
        return binding
