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


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


APP_HOST = os.getenv("CONTAINER_UP_HOST", "0.0.0.0")
APP_PORT = _int_env("CONTAINER_UP_PORT", 8080)
DB_PATH = Path(os.getenv("CONTAINER_UP_DB_PATH", "/var/lib/container_up/container_up.db"))

BUCKET_NAMESPACE = os.getenv("BUCKET_NAMESPACE", "nanobot").strip() or "nanobot"
BUCKET_NAME_PREFIX = os.getenv("BUCKET_NAME_PREFIX", "nanobot-bucket").strip() or "nanobot-bucket"
BUCKET_SERVICE_PORT = _int_env("BUCKET_SERVICE_PORT", _int_env("BUCKET_PORT", 8080))
BUCKET_CONTAINER_PORT = _int_env("BUCKET_CONTAINER_PORT", BUCKET_SERVICE_PORT)
BUCKET_REQUEST_TIMEOUT = _float_env("BUCKET_REQUEST_TIMEOUT", 120.0)
BUCKET_READY_TIMEOUT = _float_env("BUCKET_READY_TIMEOUT", 60.0)
BUCKET_MAX_INSTANCES_PER_BUCKET = _int_env("BUCKET_MAX_INSTANCES_PER_BUCKET", 20)
BUCKET_IDLE_TTL_SECONDS = _int_env("BUCKET_IDLE_TTL_SECONDS", 600)
BUCKET_IDLE_SWEEP_INTERVAL_SECONDS = _int_env("BUCKET_IDLE_SWEEP_INTERVAL_SECONDS", 60)
BUCKET_WORKSPACE_ROOT = Path(
    os.getenv("BUCKET_WORKSPACE_ROOT", "/app/nanobot_workspaces")
).expanduser()
BUCKET_BASE_URL_TEMPLATE = os.getenv("BUCKET_BASE_URL_TEMPLATE", "").strip()
BUCKET_SKIP_HEALTHCHECK = _bool_env("BUCKET_SKIP_HEALTHCHECK", False)
BUCKET_CREATE_COMMAND_TEMPLATE = os.getenv("BUCKET_CREATE_COMMAND_TEMPLATE", "").strip()
BUCKET_KUBECTL_BIN = os.getenv("BUCKET_KUBECTL_BIN", "kubectl").strip() or "kubectl"
BUCKET_RUNTIME_IMAGE = os.getenv(
    "BUCKET_RUNTIME_IMAGE",
    os.getenv("BUCKET_IMAGE", "nanobot-bucket-runtime:v1.0.0"),
).strip()
BUCKET_IMAGE_PULL_POLICY = os.getenv("BUCKET_IMAGE_PULL_POLICY", "IfNotPresent").strip()
BUCKET_SOURCE_ROOT = os.getenv("BUCKET_SOURCE_ROOT", "/mnt/nanobot/source").strip()
BUCKET_SKILLS_ROOT = os.getenv("BUCKET_SKILLS_ROOT", "/mnt/nanobot/common/default/skills").strip()
BUCKET_TEMPLATES_ROOT = os.getenv("BUCKET_TEMPLATES_ROOT", "/mnt/nanobot/common/default/templates").strip()
BUCKET_FRONTENDS_CONFIG_PATH = os.getenv(
    "BUCKET_FRONTENDS_CONFIG_PATH",
    "/mnt/nanobot/frontends/frontends.json",
).strip()
BUCKET_COMMON_MOUNT_PATH = os.getenv("BUCKET_COMMON_MOUNT_PATH", "/mnt/nanobot/common").strip()
BUCKET_FRONTENDS_MOUNT_PATH = os.getenv("BUCKET_FRONTENDS_MOUNT_PATH", "/mnt/nanobot/frontends").strip()
BUCKET_WORKSPACES_MOUNT_PATH = os.getenv("BUCKET_WORKSPACES_MOUNT_PATH", "/mnt/nanobot/workspaces").strip()
BUCKET_SOURCE_PVC = os.getenv("BUCKET_SOURCE_PVC", "nanobot-source-pvc").strip()
BUCKET_COMMON_PVC = os.getenv("BUCKET_COMMON_PVC", "nanobot-common-pvc").strip()
BUCKET_FRONTENDS_PVC = os.getenv("BUCKET_FRONTENDS_PVC", "nanobot-frontends-pvc").strip()
BUCKET_WORKSPACES_PVC = os.getenv("BUCKET_WORKSPACES_PVC", "nanobot-workspaces-pvc").strip()
BUCKET_MAX_PROCESSES = _int_env("BUCKET_MAX_PROCESSES", 30)
BUCKET_INSTANCE_IDLE_TTL_SECONDS = _int_env("BUCKET_INSTANCE_IDLE_TTL_SECONDS", 1800)
BUCKET_INSTANCE_STOP_GRACE_SECONDS = _int_env("BUCKET_INSTANCE_STOP_GRACE_SECONDS", 10)
BUCKET_INSTANCE_EVICT_INTERVAL_SECONDS = _int_env("BUCKET_INSTANCE_EVICT_INTERVAL_SECONDS", 60)
BUCKET_NANOBOT_PORT_START = _int_env("BUCKET_NANOBOT_PORT_START", 20000)
BUCKET_NANOBOT_PORT_END = _int_env("BUCKET_NANOBOT_PORT_END", 29999)

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


def build_bucket_base_url(bucket_id: str, bucket_name: str | None = None) -> str:
    bucket_name = bucket_name or bucket_name_for(bucket_id)
    if BUCKET_BASE_URL_TEMPLATE:
        return BUCKET_BASE_URL_TEMPLATE.format(
            bucket_id=bucket_id,
            bucket_name=bucket_name,
            namespace=BUCKET_NAMESPACE,
            port=BUCKET_SERVICE_PORT,
        ).rstrip("/")
    host = f"{bucket_name}.{BUCKET_NAMESPACE}.svc.cluster.local"
    return f"http://{host}:{BUCKET_SERVICE_PORT}"


def bucket_name_for(bucket_id: str) -> str:
    suffix = str(bucket_id).split("bucket-")[-1]
    return f"{BUCKET_NAME_PREFIX}-{suffix}"
