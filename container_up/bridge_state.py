from __future__ import annotations

from container_up.bridge_hub import BridgeHub


_bridge_hub: BridgeHub | None = None


def init_bridge_hub(token: str | None = None) -> BridgeHub:
    global _bridge_hub
    _bridge_hub = BridgeHub(token=token or None)
    return _bridge_hub


def get_bridge_hub() -> BridgeHub:
    if _bridge_hub is None:
        raise RuntimeError("bridge hub is not initialized")
    return _bridge_hub


def bridge_connected(org_id: str) -> bool:
    return get_bridge_hub().child_for_org(org_id) is not None
