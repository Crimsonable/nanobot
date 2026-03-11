"""Session-bound bridge state manager for container_up."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from container_up.bridge_protocol import (
    PROTOCOL_VERSION,
    REGISTER_OK_PACKET_TYPE,
    is_terminal_event,
    make_pending_key,
    make_request_id,
    make_session_key,
    parse_register_packet,
)


@dataclass
class PendingRequest:
    session_id: str
    request_id: str
    conversation_id: str
    tenant_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    done: asyncio.Future[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.done is None:
            self.done = asyncio.get_running_loop().create_future()


@dataclass
class ChildConnection:
    session_id: str
    container_name: str
    websocket: Any


class BridgeHub:
    """Minimal session-aware bridge hub for container_up."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token or None
        self._children: dict[str, ChildConnection] = {}
        self._pending: dict[tuple[str, str], PendingRequest] = {}

    @property
    def child_count(self) -> int:
        return len(self._children)

    def child_for_session(self, session_id: str) -> ChildConnection | None:
        return self._children.get(session_id)

    async def register_child(self, websocket: Any, packet: dict[str, Any]) -> str | None:
        try:
            session_id, container_name = parse_register_packet(packet)
        except ValueError:
            await websocket.close(code=4002, reason="invalid register packet")
            return None

        if self.token and packet.get("token") != self.token:
            await websocket.close(code=4003, reason="invalid token")
            return None

        self._children[session_id] = ChildConnection(
            session_id=session_id,
            container_name=container_name,
            websocket=websocket,
        )
        await websocket.send_json(
            {
                "type": REGISTER_OK_PACKET_TYPE,
                "version": PROTOCOL_VERSION,
                "session_id": session_id,
                "container_name": container_name,
            }
        )
        return session_id

    def unregister_child(self, session_id: str, websocket: Any) -> None:
        current = self._children.get(session_id)
        if current is not None and current.websocket is websocket:
            self._children.pop(session_id, None)

    async def submit_message(
        self,
        *,
        session_id: str,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        content: str,
        attachments: list[str] | None,
        metadata: dict[str, Any] | None,
        request_id: str | None,
        timeout: float,
    ) -> dict[str, Any]:
        child = self.child_for_session(session_id)
        if child is None:
            raise RuntimeError(f"No bridge channel connected for session {session_id}")

        request_id = request_id or make_request_id()
        pending = PendingRequest(
            session_id=session_id,
            request_id=request_id,
            conversation_id=conversation_id,
            tenant_id=tenant_id,
        )
        pending_key = make_pending_key(session_id, request_id)
        self._pending[pending_key] = pending
        try:
            await child.websocket.send_json(
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
                "session_id": session_id,
                "request_id": request_id,
                "conversation_id": conversation_id,
                "tenant_id": tenant_id,
                "events": pending.events,
                "result": result,
            }
        finally:
            self._pending.pop(pending_key, None)

    async def submit_cancel(
        self,
        *,
        session_id: str,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        child = self.child_for_session(session_id)
        if child is None:
            raise RuntimeError(f"No bridge channel connected for session {session_id}")

        await child.websocket.send_json(
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
            "session_id": session_id,
            "request_id": request_id,
            "conversation_id": conversation_id,
            "tenant_id": tenant_id,
        }

    async def handle_child_packet(self, session_id: str, packet: dict[str, Any]) -> None:
        request_id = str(packet.get("request_id") or "")
        if not request_id:
            return

        pending = self._pending.get(make_pending_key(session_id, request_id))
        if pending is None:
            return

        pending.events.append(packet)
        if is_terminal_event(packet) and not pending.done.done():
            pending.done.set_result(packet)
