"""Org-bound bridge connection manager for container_up."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from container_up.bridge_protocol import (
    PROTOCOL_VERSION,
    REGISTER_OK_PACKET_TYPE,
    build_register_packet,
    parse_register_packet,
)

@dataclass
class ChildConnection:
    org_id: str
    container_name: str
    websocket: Any


class BridgeHub:
    """Track child bridge connections and forward packets to them."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token or None
        self._children: dict[str, ChildConnection] = {}

    @property
    def child_count(self) -> int:
        return len(self._children)

    def child_for_org(self, org_id: str) -> ChildConnection | None:
        return self._children.get(org_id)

    async def register_child(self, websocket: Any, packet: dict[str, Any]) -> str | None:
        try:
            org_id, container_name = parse_register_packet(packet)
        except ValueError:
            await websocket.close(code=4002, reason="invalid register packet")
            return None

        if self.token and packet.get("token") != self.token:
            await websocket.close(code=4003, reason="invalid token")
            return None

        self._children[org_id] = ChildConnection(
            org_id=org_id,
            container_name=container_name,
            websocket=websocket,
        )
        await websocket.send_json(
            build_register_packet(
                org_id=org_id,
                container_name=container_name,
                token=None,
            )
            | {
                "type": REGISTER_OK_PACKET_TYPE,
                "version": PROTOCOL_VERSION,
            }
        )
        return org_id

    def unregister_child(self, org_id: str, websocket: Any) -> None:
        current = self._children.get(org_id)
        if current is not None and current.websocket is websocket:
            self._children.pop(org_id, None)

    async def submit_message(
        self,
        *,
        org_id: str,
        chat_id: str,
        usr_id: str,
        content: str,
        attachments: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        child = self.child_for_org(org_id)
        if child is None:
            raise RuntimeError(f"No bridge channel connected for org {org_id}")

        packet = {
            "type": "inbound_message",
            "version": PROTOCOL_VERSION,
            "channel": "bridge",
            "chat_id": chat_id,
            "content": content,
            "attachments": attachments or [],
            "metadata": {
                **dict(metadata or {}),
                "usr_id": usr_id,
            },
        }
        await child.websocket.send_json(packet)
        return {
            "status": "accepted",
            "org_id": org_id,
            "chat_id": chat_id,
        }

    async def submit_cancel(
        self,
        *,
        org_id: str,
        chat_id: str,
        usr_id: str,
    ) -> dict[str, Any]:
        child = self.child_for_org(org_id)
        if child is None:
            raise RuntimeError(f"No bridge channel connected for org {org_id}")

        await child.websocket.send_json(
            {
                "type": "cancel",
                "version": PROTOCOL_VERSION,
                "channel": "bridge",
                "chat_id": chat_id,
                "metadata": {
                    "usr_id": usr_id,
                },
            }
        )
        return {
            "status": "accepted",
            "org_id": org_id,
            "chat_id": chat_id,
        }

    async def handle_child_packet(self, org_id: str, packet: dict[str, Any]) -> dict[str, Any]:
        if self.child_for_org(org_id) is None:
            raise RuntimeError(f"No bridge channel connected for org {org_id}")
        return packet
