from __future__ import annotations

import os
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if raw.isdigit():
        return int(raw)
    return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw)
    except ValueError:
        return default


# Unified gateway runtime settings.
APP_HOST = os.getenv("CONTAINER_UP_HOST", "0.0.0.0")
APP_PORT = _int_env("CONTAINER_UP_PORT", 8080)
DB_PATH = Path(os.getenv("CONTAINER_UP_DB_PATH", "/var/lib/container_up/container_up.db"))

BUCKET_COUNT = _int_env("BUCKET_COUNT", 1)
BUCKET_PORT = _int_env("BUCKET_PORT", 8080)
BUCKET_REQUEST_TIMEOUT = _float_env("BUCKET_REQUEST_TIMEOUT", 120.0)
BUCKET_SERVICE_NAME = os.getenv("BUCKET_SERVICE_NAME", "nanobot-bucket").strip()
BUCKET_STATEFULSET_NAME = os.getenv(
    "BUCKET_STATEFULSET_NAME",
    BUCKET_SERVICE_NAME,
).strip() or BUCKET_SERVICE_NAME
BUCKET_NAMESPACE = os.getenv("BUCKET_NAMESPACE", "nanobot").strip() or "nanobot"
BUCKET_BASE_URL_TEMPLATE = os.getenv("BUCKET_BASE_URL_TEMPLATE", "").strip()

# Shared IM / frontend settings reused by the unified gateway and bucket runtime.
IM_PROVIDER = os.getenv("IM_PROVIDER", "qxt").strip().lower() or "qxt"

# Frontend routing config. In this branch it is the canonical frontend registry.
FRONTENDS_CONFIG_PATH = Path(
    os.getenv(
        "FRONTENDS_CONFIG_PATH",
        os.getenv("CONTAINER_UP_CONFIG_PATH", "workspace/frontends.json"),
    )
)

# Workspace paths used for attachment materialization and path normalization.
HOST_WORKSPACE_ROOT = Path(os.getenv("HOST_WORKSPACE_ROOT", "/opt/nanobot/workspaces"))
CHILD_WORKSPACE_TARGET = os.getenv("CHILD_WORKSPACE_TARGET", "/app/nanobot_workspaces")

# Shared credentials and outbound settings for the currently selected frontend.
APP_ID = os.getenv("APP_ID", os.getenv("APPID", "")).strip()
APP_SECRET = os.getenv(
    "APP_SECRET",
    os.getenv("APPSECRET", os.getenv("APPSECRECT", os.getenv("APPSCRECT", ""))),
).strip()
CORP_ID = os.getenv("CORP_ID", os.getenv("CORPID", "")).strip()
CALLBACK_TOKEN = os.getenv("CALLBACK_TOKEN", os.getenv("TOKEN", "")).strip()
ACCESS_URL = os.getenv("ACCESS_URL", "").strip()
SEND_MSG_URL = os.getenv("SEND_MSG_URL", "").strip()
SEND_MSG_TIMEOUT = _float_env("SEND_MSG_TIMEOUT", 10.0)
SEND_MSG_RETRY_COUNT = _int_env("SEND_MSG_RETRY_COUNT", 3)
SEND_MSG_RETRY_BACKOFF = _float_env("SEND_MSG_RETRY_BACKOFF", 1.0)
ATTACHMENT_URL_PREFIX = os.getenv("ATTACHMENT_URL_PREFIX", "").strip()

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", APP_ID).strip()
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", APP_SECRET).strip()
