from __future__ import annotations

from typing import Any

import httpx

from container_up.settings import BUCKET_REQUEST_TIMEOUT, build_bucket_base_url


class BucketClient:
    def __init__(self, timeout: float = BUCKET_REQUEST_TIMEOUT) -> None:
        self._timeout = timeout

    async def create_user_instance(self, bucket_url: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post(f"{bucket_url.rstrip('/')}/instances", payload)

    async def destroy_user_instance(self, bucket_url: str, instance_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.delete(
                f"{bucket_url.rstrip('/')}/instances/{instance_id}",
            )
            response.raise_for_status()
            return response.json()

    async def get_user_instance(self, bucket_url: str, instance_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{bucket_url.rstrip('/')}/instances/{instance_id}",
            )
            response.raise_for_status()
            return response.json()

    async def forward_inbound(self, bucket_url: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post(f"{bucket_url.rstrip('/')}/inbound", payload)

    async def forward_cancel(self, bucket_url: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post(f"{bucket_url.rstrip('/')}/cancel", payload)

    async def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return response.json()


def build_bucket_url(bucket_id: str) -> str:
    return build_bucket_base_url(bucket_id)
