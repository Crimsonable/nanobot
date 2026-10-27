from __future__ import annotations

import os


def _required_env(name: str) -> str:
    raw = os.getenv(name, "").strip()
    if raw:
        return raw
    raise RuntimeError(f"missing required environment variable: {name}")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    port = int(raw)
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid port: {name}")
    return port


APP_HOST = os.getenv("WEB_SERVER_HOST", "0.0.0.0").strip() or "0.0.0.0"
APP_PORT = _int_env("WEB_SERVER_PORT", 8000)
GATEWAY_BASE_URL = _required_env("GATEWAY_BASE_URL").rstrip("/")
OUTBOUND_ECHO = os.getenv("WEB_SERVER_OUTBOUND_ECHO", "1").strip() not in {"0", "false", "False"}
