from __future__ import annotations

import json
from typing import Any

from channel_gateway.http_state import get_dispatch_session


class WebIMParser:
    provider = "web"

    def __init__(self, *, frontend_id: str, frontend_config: dict[str, Any]) -> None:
        self.frontend_id = frontend_id
        self.send_msg_url = str(frontend_config.get("send_msg_url", "")).strip()

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def supports_subscribe(self) -> bool:
        return False


    async def post_message(
        self, *, payload: dict[str, object]
    ) -> dict[str, object]:
        if not self.send_msg_url:
            raise RuntimeError("web send_msg_url is not configured")

        metadata = dict(payload.get("metadata") or {})
        user_id = str(metadata.get("user_id") or "").strip()
        attachments = list(payload.get("attachments") or [])
        outbound_payload = {
            "frontend_id": self.frontend_id,
            "user_id": user_id,
            "chat_id": str(payload.get("chat_id") or ""),
            "content": str(payload.get("content") or ""),
            "attachments": attachments,
            "metadata": metadata,
        }

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
