from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import ClientError

from container_up.http_state import get_dispatch_session
from container_up.outbound_attachment_store import prepare_outbound_attachments
from container_up.settings import SEND_MSG_RETRY_BACKOFF, SEND_MSG_RETRY_COUNT


class WebIMParser:
    provider = "web"

    def __init__(
        self,
        *,
        frontend_id: str = "default",
        send_msg_url: str | None = None,
        frontend_config: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        self.frontend_id = frontend_id
        config = dict(frontend_config or {})
        self.frontend_config = config
        self.send_msg_retry_count = int(
            config.get("send_msg_retry_count") or SEND_MSG_RETRY_COUNT
        )
        self.send_msg_retry_backoff = float(
            config.get("send_msg_retry_backoff") or SEND_MSG_RETRY_BACKOFF
        )
        self.send_msg_url = str(
            send_msg_url
            or config.get("send_msg_url")
            or config.get("outbound_url")
            or ""
        ).strip()

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def supports_subscribe(self) -> bool:
        return False

    async def prepare_inbound_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    async def post_message_with_retry(
        self, *, payload: dict[str, object]
    ) -> dict[str, object]:
        if not self.send_msg_url:
            raise RuntimeError("web send_msg_url is not configured")

        metadata = dict(payload.get("metadata") or {})
        frontend_id = str(metadata.get("frontend_id") or self.frontend_id).strip() or self.frontend_id
        user_id = str(metadata.get("usr_id") or "").strip()
        outbound_payload = {
            "frontend_id": frontend_id,
            "user_id": user_id,
            "chat_id": str(payload.get("chat_id") or ""),
            "content": str(payload.get("content") or ""),
            "attachments": await prepare_outbound_attachments(
                list(payload.get("attachments") or []),
                frontend_id=frontend_id,
                user_id=user_id,
                frontend_config=self.frontend_config,
            ),
            "metadata": metadata,
        }

        last_error: Exception | None = None
        for attempt in range(1, self.send_msg_retry_count + 1):
            try:
                async with get_dispatch_session().post(
                    self.send_msg_url,
                    json=outbound_payload,
                ) as response:
                    response_text = await response.text()
                    if response.status >= 500:
                        raise RuntimeError(
                            f"web outbound failed with {response.status}: {response_text}"
                        )
                    if response.status >= 400:
                        raise RuntimeError(
                            f"web outbound rejected with {response.status}: {response_text}"
                        )
                    content_type = str(response.headers.get("Content-Type") or "")
                    if "application/json" in content_type:
                        return (
                            json.loads(response_text)
                            if response_text.strip()
                            else {"status": "accepted"}
                        )
                    return {"status": "accepted", "response_text": response_text}
            except (
                asyncio.TimeoutError,
                ClientError,
                RuntimeError,
                json.JSONDecodeError,
            ) as exc:
                last_error = exc
                if attempt >= self.send_msg_retry_count:
                    break
                await asyncio.sleep(self.send_msg_retry_backoff * attempt)
        if last_error is not None:
            raise last_error
        raise RuntimeError("web outbound failed without an explicit error")
