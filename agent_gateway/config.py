from __future__ import annotations

import os
from pathlib import Path


APP_HOST = os.getenv("AGENT_GATEWAY_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("AGENT_GATEWAY_PORT", "8080"))
DB_PATH = Path(
    os.getenv("AGENT_GATEWAY_DB_PATH", "/var/lib/agent_gateway/agent_gateway.db")
)

BUCKET_COUNT = int(os.getenv("BUCKET_COUNT", "1"))
BUCKET_PORT = int(os.getenv("BUCKET_PORT", "8080"))
BUCKET_REQUEST_TIMEOUT = float(os.getenv("BUCKET_REQUEST_TIMEOUT", "120"))
BUCKET_SERVICE_NAME = os.getenv("BUCKET_SERVICE_NAME", "nanobot-bucket").strip()
BUCKET_STATEFULSET_NAME = os.getenv(
    "BUCKET_STATEFULSET_NAME",
    BUCKET_SERVICE_NAME,
).strip() or BUCKET_SERVICE_NAME
BUCKET_NAMESPACE = os.getenv("BUCKET_NAMESPACE", "nanobot").strip() or "nanobot"
BUCKET_BASE_URL_TEMPLATE = os.getenv("BUCKET_BASE_URL_TEMPLATE", "").strip()
