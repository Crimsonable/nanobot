"""Wait for a bridge reply without changing the existing outbound endpoints."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from starlette.responses import JSONResponse

from container_up.frontend_config import frontend_config_for
from container_up.outbound_attachment_store import prepare_outbound_attachments


REQUEST_ID_KEY = "_sync_request_id"
_OUTBOUND_PATHS = {"/outbound", "/api/bridge/outbound"}
_LATE_REPLY_TTL = 600.0


@dataclass
class PendingReply:
    frontend_id: str
    user_id: str
    chat_id: str
    future: asyncio.Future[dict[str, Any]]
    handling: bool = False


class SyncReplyBroker:
    def __init__(self) -> None:
        self._pending: dict[str, PendingReply] = {}
        self._finished: dict[str, float] = {}

    def register(self, *, frontend_id: str, user_id: str, chat_id: str) -> tuple[str, PendingReply]:
        now = time.monotonic()
        self._finished = {
            key: expires for key, expires in self._finished.items() if expires > now
        }
        request_id = uuid4().hex
        pending = PendingReply(
            frontend_id=frontend_id,
            user_id=user_id,
            chat_id=chat_id,
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[request_id] = pending
        return request_id, pending

    def finish(self, request_id: str) -> None:
        pending = self._pending.pop(request_id, None)
        if pending is not None and not pending.future.done():
            pending.future.cancel()
        self._finished[request_id] = time.monotonic() + _LATE_REPLY_TTL

    async def consume(self, packet: dict[str, Any]) -> bool:
        """Claim only replies for this endpoint; let all other traffic continue unchanged."""
        raw_metadata = packet.get("metadata")
        if not isinstance(raw_metadata, dict):
            return False
        metadata = dict(raw_metadata)
        request_id = str(metadata.get(REQUEST_ID_KEY) or "")
        if not request_id:
            return False

        pending = self._pending.get(request_id)
        if pending is None:
            return self._finished.get(request_id, 0) > time.monotonic()

        frontend_id = str(packet.get("frontend_id") or metadata.get("frontend_id") or "")
        user_id = str(packet.get("user_id") or metadata.get("usr_id") or "")
        chat_id = str(packet.get("chat_id") or packet.get("to") or "")
        if (frontend_id, user_id, chat_id) != (
            pending.frontend_id, pending.user_id, pending.chat_id
        ):
            return False

        if metadata.get("_progress") or metadata.get("_stream_delta") or metadata.get("_stream_end"):
            return True
        if pending.handling:
            return True
        pending.handling = True

        try:
            config = frontend_config_for(frontend_id)
            attachments = await prepare_outbound_attachments(
                list(packet.get("attachments") or []),
                frontend_id=frontend_id,
                user_id=user_id,
                frontend_config=config.raw if config is not None else None,
            )
            metadata.pop(REQUEST_ID_KEY, None)
            if not pending.future.done():
                pending.future.set_result(
                    {
                        "content": str(packet.get("content") or ""),
                        "attachments": attachments,
                        "metadata": metadata,
                    }
                )
        except Exception as exc:
            if not pending.future.done():
                pending.future.set_exception(exc)
        return True


class SyncReplyMiddleware:
    """Consume correlated outbound HTTP packets before the existing route handles them."""

    def __init__(self, app: Any, broker: SyncReplyBroker) -> None:
        self.app = app
        self.broker = broker

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in _OUTBOUND_PATHS:
            await self.app(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break

        try:
            packet = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            packet = None

        required = (
            ("frontend_id", "user_id", "chat_id", "content")
            if scope["path"] == "/outbound"
            else ("to", "content")
        )
        valid_packet = (
            isinstance(packet, dict)
            and all(isinstance(packet.get(key), str) for key in required)
            and isinstance(packet.get("attachments", []), list)
            and isinstance(packet.get("metadata", {}), dict)
        )
        if valid_packet and await self.broker.consume(packet):
            await JSONResponse({"ok": True, "response": None})(scope, receive, send)
            return

        replayed = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)
