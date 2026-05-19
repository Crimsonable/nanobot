from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse, urlunparse


def _required_env(name: str) -> str:
    raw = os.getenv(name, "").strip()
    if raw:
        return raw
    raise RuntimeError(f"missing required environment variable: {name}")


def _derive_url(base_url: str, path: str) -> str:
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


APP_HOST = os.getenv("BUCKET_RUNTIME_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("BUCKET_RUNTIME_PORT", "8080"))

BUCKET_MOUNT_ROOT = Path(_required_env("BUCKET_MOUNT_ROOT")).expanduser()
SOURCE_ROOT = Path(_required_env("SOURCE_ROOT")).expanduser()
COMMON_ROOT = Path(_required_env("BUCKET_COMMON_ROOT")).expanduser()
CONTAINER_UP_SOURCE_ROOT = SOURCE_ROOT / "container_up"
BUCKET_RUNTIME_SOURCE_ROOT = SOURCE_ROOT / "bucket_runtime"
NANOBOT_SOURCE_ROOT = SOURCE_ROOT / "nanobot"
WORKSPACE_ROOT = BUCKET_MOUNT_ROOT / "workspaces"

INSTANCE_HOST = os.getenv("INSTANCE_HOST", "127.0.0.1")
CONTAINER_UP_BASE_URL = os.getenv(
    "CONTAINER_UP_BASE_URL",
    "http://container-up.nanobot.svc.cluster.local:8080",
).strip().rstrip("/")
CONTAINER_UP_OUTBOUND_URL = _derive_url(CONTAINER_UP_BASE_URL, "/outbound")
CONTAINER_UP_RUNTIME_RELEASE_URL = _derive_url(
    CONTAINER_UP_BASE_URL,
    "/internal/runtime/release",
)
CONTAINER_UP_ATTACHMENT_UPLOAD_URL = _derive_url(
    CONTAINER_UP_BASE_URL,
    "/internal/attachments/upload",
)
CONTAINER_UP_BRIDGE_OUTBOUND_URL = _derive_url(
    CONTAINER_UP_BASE_URL,
    "/api/bridge/outbound",
)
OUTBOUND_TIMEOUT = float(os.getenv("OUTBOUND_TIMEOUT_SECONDS", "120"))
CONTROL_REQUEST_TIMEOUT = float(os.getenv("CONTROL_REQUEST_TIMEOUT_SECONDS", "15"))

INSTANCE_IDLE_TTL_SECONDS = int(os.getenv("INSTANCE_IDLE_TTL_SECONDS", "1800"))
INSTANCE_STOP_GRACE_SECONDS = int(os.getenv("INSTANCE_STOP_GRACE_SECONDS", "10"))
INSTANCE_EVICT_INTERVAL_SECONDS = int(os.getenv("INSTANCE_EVICT_INTERVAL_SECONDS", "60"))
MAX_PROCESSES_PER_BUCKET = int(os.getenv("MAX_PROCESSES_PER_BUCKET", "30"))
NANOBOT_PORT_START = int(os.getenv("NANOBOT_PORT_START", "20000"))
NANOBOT_PORT_END = int(os.getenv("NANOBOT_PORT_END", "29999"))

POD_NAME = os.getenv("POD_NAME", "").strip()
BUCKET_ID = os.getenv("BUCKET_ID", "").strip()
if not BUCKET_ID and POD_NAME:
    match = re.search(r"(\d+)$", POD_NAME)
    if match:
        BUCKET_ID = f"bucket-{match.group(1)}"
if not BUCKET_ID:
    BUCKET_ID = "bucket-0"
