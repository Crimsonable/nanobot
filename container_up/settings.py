from __future__ import annotations

import os
from pathlib import Path


def _required_env(name: str) -> str:
    raw = os.getenv(name, "").strip()
    if raw:
        return raw
    raise RuntimeError(f"missing required environment variable: {name}")


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


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


APP_HOST = os.getenv("CONTAINER_UP_HOST", "0.0.0.0")
APP_PORT = _int_env("CONTAINER_UP_PORT", 8080)
BUCKET_MOUNT_ROOT = Path(_required_env("BUCKET_MOUNT_ROOT")).expanduser()
BUCKET_MOUNT_PVC = _required_env("BUCKET_MOUNT_PVC")
SOURCE_ROOT = Path(_required_env("SOURCE_ROOT")).expanduser()
SOURCE_PVC = _required_env("SOURCE_PVC")
CONTAINER_UP_SOURCE_ROOT = SOURCE_ROOT / "container_up"
BUCKET_RUNTIME_SOURCE_ROOT = SOURCE_ROOT / "bucket_runtime"
NANOBOT_SOURCE_ROOT = SOURCE_ROOT / "nanobot"
BUCKET_COMMON_ROOT = BUCKET_MOUNT_ROOT / "common"
BUCKET_ROUTE_DB_ROOT = BUCKET_MOUNT_ROOT / "routedb"
BUCKET_WORKSPACE_ROOT = BUCKET_MOUNT_ROOT / "workspaces"
DB_PATH = BUCKET_ROUTE_DB_ROOT / "container_up.db"
FRONTENDS_CONFIG_PATH = BUCKET_COMMON_ROOT / "frontends.json"
HOST_WORKSPACE_ROOT = BUCKET_WORKSPACE_ROOT
CHILD_WORKSPACE_TARGET = str(BUCKET_WORKSPACE_ROOT)

BUCKET_NAMESPACE = os.getenv("BUCKET_NAMESPACE", "nanobot").strip() or "nanobot"
BUCKET_NAME_PREFIX = os.getenv("BUCKET_NAME_PREFIX", "nanobot-bucket").strip() or "nanobot-bucket"
BUCKET_SERVICE_PORT = _int_env("BUCKET_SERVICE_PORT", _int_env("BUCKET_PORT", 8080))
BUCKET_CONTAINER_PORT = _int_env("BUCKET_CONTAINER_PORT", BUCKET_SERVICE_PORT)
BUCKET_REQUEST_TIMEOUT = _float_env("BUCKET_REQUEST_TIMEOUT", 120.0)
BUCKET_READY_TIMEOUT = _float_env("BUCKET_READY_TIMEOUT", 60.0)
BUCKET_MAX_INSTANCES_PER_BUCKET = _int_env("BUCKET_MAX_INSTANCES_PER_BUCKET", 20)
BUCKET_IDLE_TTL_SECONDS = _int_env("BUCKET_IDLE_TTL_SECONDS", 600)
BUCKET_IDLE_SWEEP_INTERVAL_SECONDS = _int_env("BUCKET_IDLE_SWEEP_INTERVAL_SECONDS", 60)
BUCKET_BASE_URL_TEMPLATE = os.getenv("BUCKET_BASE_URL_TEMPLATE", "").strip()
BUCKET_SKIP_HEALTHCHECK = _bool_env("BUCKET_SKIP_HEALTHCHECK", False)
BUCKET_CREATE_COMMAND_TEMPLATE = os.getenv("BUCKET_CREATE_COMMAND_TEMPLATE", "").strip()
BUCKET_KUBECTL_BIN = os.getenv("BUCKET_KUBECTL_BIN", "kubectl").strip() or "kubectl"
BUCKET_RUNTIME_IMAGE = os.getenv(
    "BUCKET_RUNTIME_IMAGE",
    os.getenv("BUCKET_IMAGE", "nanobot-bucket-runtime:v1.0.0"),
).strip()
BUCKET_IMAGE_PULL_POLICY = os.getenv("BUCKET_IMAGE_PULL_POLICY", "IfNotPresent").strip()
BUCKET_MAX_PROCESSES = _int_env("BUCKET_MAX_PROCESSES", 30)
BUCKET_INSTANCE_IDLE_TTL_SECONDS = _int_env("BUCKET_INSTANCE_IDLE_TTL_SECONDS", 1800)
BUCKET_INSTANCE_STOP_GRACE_SECONDS = _int_env("BUCKET_INSTANCE_STOP_GRACE_SECONDS", 10)
BUCKET_INSTANCE_EVICT_INTERVAL_SECONDS = _int_env("BUCKET_INSTANCE_EVICT_INTERVAL_SECONDS", 60)
BUCKET_NANOBOT_PORT_START = _int_env("BUCKET_NANOBOT_PORT_START", 20000)
BUCKET_NANOBOT_PORT_END = _int_env("BUCKET_NANOBOT_PORT_END", 29999)

# Shared IM / frontend settings reused by the unified gateway and bucket runtime.
IM_PROVIDER = os.getenv("IM_PROVIDER", "qxt").strip().lower() or "qxt"

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


def build_bucket_base_url(bucket_id: str, bucket_name: str | None = None) -> str:
    bucket_name = bucket_name or bucket_name_for(bucket_id)
    if BUCKET_BASE_URL_TEMPLATE:
        return BUCKET_BASE_URL_TEMPLATE.format(
            bucket_id=bucket_id,
            bucket_name=bucket_name,
            namespace=BUCKET_NAMESPACE,
            port=BUCKET_SERVICE_PORT,
        ).rstrip("/")
    host = f"{bucket_name}.{BUCKET_NAMESPACE}"
    return f"http://{host}:{BUCKET_SERVICE_PORT}"


def bucket_name_for(bucket_id: str) -> str:
    suffix = str(bucket_id).split("bucket-")[-1]
    return f"{BUCKET_NAME_PREFIX}-{suffix}"
