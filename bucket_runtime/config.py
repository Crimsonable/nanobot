from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse, urlunparse


def _derive_release_url(outbound_url: str) -> str:
    parsed = urlparse(outbound_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return urlunparse(
        (parsed.scheme, parsed.netloc, "/internal/runtime/release", "", "", "")
    )


APP_HOST = os.getenv("BUCKET_RUNTIME_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("BUCKET_RUNTIME_PORT", "8080"))

SOURCE_ROOT = Path(os.getenv("SOURCE_ROOT", "/mnt/nanobot/source"))
_default_config_path = os.getenv("DEFAULT_CONFIG_PATH", "").strip()
DEFAULT_CONFIG_PATH = Path(_default_config_path) if _default_config_path else None
SKILLS_ROOT = Path(os.getenv("SKILLS_ROOT", "/mnt/nanobot/skills"))
TEMPLATES_ROOT = Path(os.getenv("TEMPLATES_ROOT", "/mnt/nanobot/templates"))
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", "/mnt/nanobot/workspaces"))

INSTANCE_HOST = os.getenv("INSTANCE_HOST", "127.0.0.1")
OUTBOUND_GATEWAY_URL = os.getenv(
    "OUTBOUND_GATEWAY_URL",
    "http://container-up.nanobot.svc.cluster.local:8080/outbound",
).strip()
OUTBOUND_TIMEOUT = float(os.getenv("OUTBOUND_TIMEOUT_SECONDS", "120"))
RELEASE_GATEWAY_URL = os.getenv(
    "RELEASE_GATEWAY_URL",
    "",
).strip() or _derive_release_url(OUTBOUND_GATEWAY_URL)
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
