"""Shared protocol helpers for the standalone bridge service."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

PROTOCOL_VERSION = 1


def make_request_id() -> str:
    """Generate a request id for correlating a user request across the bridge."""
    return f"req_{uuid4().hex}"


def make_session_key(tenant_id: str, conversation_id: str) -> str:
    """Build the session key expected by nanobot's bridge channel."""
    tenant = tenant_id or "default"
    return f"remote:{tenant}:{conversation_id}"


def encode_packet(packet: dict[str, Any]) -> str:
    """Serialize a packet to JSON."""
    return json.dumps(packet, ensure_ascii=False)


def decode_packet(raw: str) -> dict[str, Any]:
    """Deserialize a packet from JSON."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Packet must be a JSON object")
    return data
