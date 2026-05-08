from __future__ import annotations

import os


os.environ.setdefault("BUCKET_MOUNT_ROOT", "/mnt/nanobot")
os.environ.setdefault("BUCKET_MOUNT_PVC", "nanobot-data-pvc")
os.environ.setdefault("SOURCE_ROOT", "/mnt/nanobot/source")
os.environ.setdefault("SOURCE_PVC", "nanobot-source-pvc")
os.environ.setdefault("CONTAINER_UP_BASE_URL", "http://container-up.nanobot:8080")
os.environ.setdefault(
    "CONTAINER_UP_ATTACHMENT_UPLOAD_URL",
    "http://container-up.nanobot:8080/internal/attachments/upload",
)
os.environ.setdefault(
    "CONTAINER_UP_BRIDGE_OUTBOUND_URL",
    "http://container-up.nanobot:8080/api/bridge/outbound",
)
