from __future__ import annotations

import os
import re
from pathlib import Path


APP_HOST = os.getenv("BUCKET_RUNTIME_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("BUCKET_RUNTIME_PORT", "8080"))

SOURCE_ROOT = Path(os.getenv("SOURCE_ROOT", "/mnt/nanobot/source"))
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/mnt/nanobot/config/config.json"))
SKILLS_ROOT = Path(os.getenv("SKILLS_ROOT", "/mnt/nanobot/skills"))
TEMPLATES_ROOT = Path(os.getenv("TEMPLATES_ROOT", "/mnt/nanobot/templates"))
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", "/mnt/nanobot/workspaces"))

INSTANCE_HOST = os.getenv("INSTANCE_HOST", "127.0.0.1")
OUTBOUND_GATEWAY_URL = os.getenv(
    "OUTBOUND_GATEWAY_URL",
    "http://agent-gateway.nanobot.svc.cluster.local:8080/outbound",
).strip()
OUTBOUND_TIMEOUT = float(os.getenv("OUTBOUND_TIMEOUT_SECONDS", "120"))

INSTANCE_IDLE_TTL_SECONDS = int(os.getenv("INSTANCE_IDLE_TTL_SECONDS", "1800"))
INSTANCE_STOP_GRACE_SECONDS = int(os.getenv("INSTANCE_STOP_GRACE_SECONDS", "10"))
INSTANCE_EVICT_INTERVAL_SECONDS = int(os.getenv("INSTANCE_EVICT_INTERVAL_SECONDS", "60"))
MAX_PROCESSES_PER_BUCKET = int(os.getenv("MAX_PROCESSES_PER_BUCKET", "30"))
NANOBOT_PORT_START = int(os.getenv("NANOBOT_PORT_START", "20000"))
NANOBOT_PORT_END = int(os.getenv("NANOBOT_PORT_END", "29999"))

POD_NAME = os.getenv("POD_NAME", "").strip()
BUCKET_ID = int(os.getenv("BUCKET_ID", "-1"))
if BUCKET_ID < 0 and POD_NAME:
    match = re.search(r"(\d+)$", POD_NAME)
    if match:
        BUCKET_ID = int(match.group(1))
if BUCKET_ID < 0:
    BUCKET_ID = 0
