"""Minimal bridge state for HTTP users and nanobot bot-side WebSocket."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from bridge_service.protocol import PROTOCOL_VERSION, make_request_id, make_session_key

TERMINAL_EVENT_TYPES = {"final", "error", "cancelled"}


@dataclass
class PendingRequest:
    request_id: str
    conversation_id: str
    tenant_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    done: asyncio.Future[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.done is None:
            self.done = asyncio.get_running_loop().create_future()


class BridgeCore:
    """Minimal state manager for the bridge service."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token or None
        self._bots: set[Any] = set()
        self._pending: dict[str, PendingRequest] = {}

    @property
    def bot_count(self) -> int:
        return len(self._bots)

    async def authenticate_ws(self, websocket: Any, packet: dict[str, Any]) -> bool:
        if not self.token:
            return True
        if packet.get("type") != "auth" or packet.get("token") != self.token:
            await websocket.close(code=4003, reason="invalid token")
            return False
        await websocket.send_json({"type": "auth_ok"})
        return True

    def register_bot(self, websocket: Any) -> None:
        self._bots.add(websocket)

    def unregister_bot(self, websocket: Any) -> None:
        self._bots.discard(websocket)

    async def submit_message(
        self,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        content: str,
        attachments: list[str] | None,
        metadata: dict[str, Any] | None,
        request_id: str | None,
        timeout: float,
    ) -> dict[str, Any]:
        bot = self._pick_bot()
        if bot is None:
            raise RuntimeError("No nanobot bridge channel connected")

        request_id = request_id or make_request_id()
        pending = PendingRequest(
            request_id=request_id,
            conversation_id=conversation_id,
            tenant_id=tenant_id,
        )
        self._pending[request_id] = pending
        try:
            await bot.send_json(
                {
                    "type": "inbound_message",
                    "version": PROTOCOL_VERSION,
                    "request_id": request_id,
                    "tenant_id": tenant_id,
                    "conversation_id": conversation_id,
                    "session_key": make_session_key(tenant_id, conversation_id),
                    "channel": "bridge",
                    "sender_id": user_id,
                    "chat_id": conversation_id,
                    "content": content,
                    "attachments": attachments or [],
                    "metadata": metadata or {},
                }
            )
            result = await asyncio.wait_for(pending.done, timeout=timeout)
            return {
                "request_id": request_id,
                "conversation_id": conversation_id,
                "tenant_id": tenant_id,
                "events": pending.events,
                "result": result,
            }
        finally:
            self._pending.pop(request_id, None)

    async def submit_cancel(
        self,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        bot = self._pick_bot()
        if bot is None:
            raise RuntimeError("No nanobot bridge channel connected")

        await bot.send_json(
            {
                "type": "cancel",
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "session_key": make_session_key(tenant_id, conversation_id),
                "sender_id": user_id,
            }
        )
        return {
            "status": "accepted",
            "request_id": request_id,
            "conversation_id": conversation_id,
            "tenant_id": tenant_id,
        }

    async def handle_bot_packet(self, packet: dict[str, Any]) -> None:
        request_id = str(packet.get("request_id") or "")
        if not request_id:
            return

        pending = self._pending.get(request_id)
        if pending is None:
            return

        pending.events.append(packet)
        if packet.get("type") in TERMINAL_EVENT_TYPES and not pending.done.done():
            pending.done.set_result(packet)

    def _pick_bot(self) -> Any | None:
        return next(iter(self._bots), None)
