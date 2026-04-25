from __future__ import annotations

from typing import Any

import httpx

from container_up.settings import (
    BUCKET_BASE_URL_TEMPLATE,
    BUCKET_NAMESPACE,
    BUCKET_PORT,
    BUCKET_REQUEST_TIMEOUT,
    BUCKET_SERVICE_NAME,
    BUCKET_STATEFULSET_NAME,
)


def build_bucket_base_url(bucket_id: int) -> str:
    if BUCKET_BASE_URL_TEMPLATE:
        return BUCKET_BASE_URL_TEMPLATE.format(
            bucket_id=bucket_id,
            service_name=BUCKET_SERVICE_NAME,
            statefulset_name=BUCKET_STATEFULSET_NAME,
            namespace=BUCKET_NAMESPACE,
            port=BUCKET_PORT,
        ).rstrip("/")
    host = (
        f"{BUCKET_STATEFULSET_NAME}-{bucket_id}."
        f"{BUCKET_SERVICE_NAME}.{BUCKET_NAMESPACE}.svc.cluster.local"
    )
    return f"http://{host}:{BUCKET_PORT}"


class BucketClient:
    def __init__(self, timeout: float = BUCKET_REQUEST_TIMEOUT) -> None:
        self._timeout = timeout

    async def forward_inbound(self, bucket_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{build_bucket_base_url(bucket_id)}/inbound",
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def forward_cancel(self, bucket_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{build_bucket_base_url(bucket_id)}/cancel",
                json=payload,
            )
            response.raise_for_status()
            return response.json()
