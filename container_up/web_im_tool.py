from __future__ import annotations

import json
from typing import Any

from container_up.attachment_paths import normalize_outbound_attachments
from container_up.http_state import get_dispatch_session


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
        outbound_payload = {
            "frontend_id": frontend_id,
            "user_id": str(metadata.get("usr_id") or ""),
            "chat_id": str(payload.get("chat_id") or ""),
            "content": str(payload.get("content") or ""),
            "attachments": normalize_outbound_attachments(
                list(payload.get("attachments") or []),
                frontend_id=frontend_id,
            ),
            "metadata": metadata,
        }

        async with get_dispatch_session().post(
            self.send_msg_url,
            json=outbound_payload,
        ) as response:
            response_text = await response.text()
            if response.status >= 400:
                raise RuntimeError(
                    f"web outbound rejected with {response.status}: {response_text}"
                )
            content_type = str(response.headers.get("Content-Type") or "")
            if "application/json" in content_type:
                return json.loads(response_text) if response_text.strip() else {"status": "accepted"}
            return {"status": "accepted", "response_text": response_text}
