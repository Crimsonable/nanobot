"""Shared bridge protocol helpers for container_up <-> child org containers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PROTOCOL_VERSION = 2

REGISTER_PACKET_TYPE = "register"
REGISTER_OK_PACKET_TYPE = "register_ok"


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

    org_id = str(packet.get("org_id") or "").strip()
    if not org_id:
        raise ValueError("missing org_id")

    container_name = str(packet.get("container_name") or "").strip()
    if not container_name:
        raise ValueError("missing container_name")

    return org_id, container_name
