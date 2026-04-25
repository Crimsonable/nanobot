from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from container_up.frontend_config import safe_frontend_id
from container_up.settings import CHILD_WORKSPACE_TARGET, HOST_WORKSPACE_ROOT

ATTACHMENTS_CACHE_DIR = Path("cache") / "attachments"
WORKSPACE_LAYOUT = (
    os.getenv("NANOBOT_WORKSPACE_LAYOUT", "legacy-org-user").strip().lower()
    or "legacy-org-user"
)


def safe_workspace_component(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value).strip(
        "-."
    )
    return cleaned[:96] or "item"


def safe_instance_name(value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value).strip(
        "-."
    )
    return f"{cleaned[:48] or 'user'}-{digest}"


def host_instance_workspace_path(
    user_id: str,
    frontend_id: str | None = None,
) -> Path:
    if WORKSPACE_LAYOUT == "frontend-user" and frontend_id:
        return (
            HOST_WORKSPACE_ROOT
            / safe_frontend_id(frontend_id)
            / safe_workspace_component(user_id)
        ).resolve(strict=False)
    return (HOST_WORKSPACE_ROOT / safe_instance_name(user_id)).resolve(strict=False)


def child_instance_workspace_path(
    user_id: str,
    frontend_id: str | None = None,
) -> Path:
    child_root = Path(CHILD_WORKSPACE_TARGET)
    if WORKSPACE_LAYOUT == "frontend-user" and frontend_id:
        return (
            child_root / safe_frontend_id(frontend_id) / safe_workspace_component(user_id)
        ).resolve(strict=False)
    return (child_root / safe_instance_name(user_id)).resolve(strict=False)


def host_attachment_cache_dir(
    *,
    user_id: str,
    attachment_group: str,
    provider: str,
    frontend_id: str | None = None,
) -> Path:
    return (
        host_instance_workspace_path(user_id, frontend_id=frontend_id)
        / ATTACHMENTS_CACHE_DIR
        / provider
        / safe_instance_name(attachment_group)
    ).resolve(strict=False)


def child_attachment_cache_dir(
    *,
    user_id: str,
    attachment_group: str,
    provider: str,
    frontend_id: str | None = None,
) -> Path:
    return (
        child_instance_workspace_path(user_id, frontend_id=frontend_id)
        / ATTACHMENTS_CACHE_DIR
        / provider
        / safe_instance_name(attachment_group)
    ).resolve(strict=False)


def child_attachment_to_host_path(
    attachment: Any,
    *,
    frontend_id: str | None = None,
) -> Any:
    if isinstance(attachment, dict):
        url = str(attachment.get("url") or "").strip()
        if not url or url.startswith(("http://", "https://")):
            return attachment
        mapped = dict(attachment)
        mapped["url"] = child_attachment_to_host_path(
            url,
            frontend_id=frontend_id,
        )
        return mapped

    if not isinstance(attachment, str):
        return attachment

    text = attachment.strip()
    if not text or text.startswith(("http://", "https://")):
        return text

    child_root = Path(CHILD_WORKSPACE_TARGET).expanduser().resolve(strict=False)
    child_path = Path(text).expanduser()
    if not child_path.is_absolute():
        return text

    try:
        relative = child_path.resolve(strict=False).relative_to(child_root)
    except ValueError:
        return text

    if WORKSPACE_LAYOUT == "frontend-user" and frontend_id:
        host_path = (HOST_WORKSPACE_ROOT / relative).resolve(strict=False)
    else:
        host_path = (HOST_WORKSPACE_ROOT / relative).resolve(strict=False)
    return str(host_path)


def normalize_outbound_attachments(
    attachments: list[Any] | None = None,
    *,
    frontend_id: str | None = None,
) -> list[Any]:
    attachments = attachments or []
    return [
        child_attachment_to_host_path(
            item,
            frontend_id=frontend_id,
        )
        for item in attachments
    ]
