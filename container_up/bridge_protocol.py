"""Shared bridge protocol helpers for container_up <-> child org containers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

PROTOCOL_VERSION = 2

REGISTER_PACKET_TYPE = "register"
REGISTER_OK_PACKET_TYPE = "register_ok"
REGISTER_REJECT_PACKET_TYPE = "register_reject"

TERMINAL_EVENT_TYPES = {"final", "error", "cancelled"}


def make_request_id() -> str:
    """Generate a request id for a user request."""
    return f"req_{uuid4().hex}"


def make_session_key(tenant_id: str, conversation_id: str) -> str:
    """Build the session key expected by nanobot's bridge channel."""
    tenant = tenant_id or "default"
    return f"remote:{tenant}:{conversation_id}"


def make_pending_key(org_id: str, request_id: str) -> tuple[str, str]:
    """Build a collision-safe key for pending requests inside container_up."""
    return org_id, request_id


def build_register_packet(
    *,
    org_id: str,
    container_name: str,
    token: str | None = None,
) -> dict[str, Any]:
    """Create the initial registration packet sent by a child bridge channel."""
    packet: dict[str, Any] = {
        "type": REGISTER_PACKET_TYPE,
        "version": PROTOCOL_VERSION,
        "org_id": org_id,
        "container_name": container_name,
    }
    if token:
        packet["token"] = token
    return packet


def parse_register_packet(packet: Mapping[str, Any]) -> tuple[str, str]:
    """Validate and extract child registration fields."""
    if packet.get("type") != REGISTER_PACKET_TYPE:
        raise ValueError("expected register packet")

    org_id = str(packet.get("org_id") or packet.get("session_id") or "").strip()
    if not org_id:
        raise ValueError("missing org_id")

    container_name = str(packet.get("container_name") or "").strip()
    if not container_name:
        raise ValueError("missing container_name")

    return org_id, container_name


def is_terminal_event(packet: Mapping[str, Any]) -> bool:
    """Return True when a bridge event completes a request lifecycle."""
    return str(packet.get("type") or "") in TERMINAL_EVENT_TYPES
