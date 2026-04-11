from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from container_up.settings import CHILD_WORKSPACE_TARGET, HOST_WORKSPACE_ROOT

ATTACHMENTS_CACHE_DIR = Path("cache") / "attachments"


def safe_instance_name(value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value).strip(
        "-."
    )
    return f"{cleaned[:48] or 'user'}-{digest}"


def host_instance_workspace_path(org_id: str, user_id: str) -> Path:
    return (HOST_WORKSPACE_ROOT / org_id / safe_instance_name(user_id)).resolve(strict=False)


def child_instance_workspace_path(user_id: str) -> Path:
    return (Path(CHILD_WORKSPACE_TARGET) / safe_instance_name(user_id)).resolve(strict=False)


def host_attachment_cache_dir(
    *,
    org_id: str,
    user_id: str,
    attachment_group: str,
    provider: str,
) -> Path:
    return (
        host_instance_workspace_path(org_id, user_id)
        / ATTACHMENTS_CACHE_DIR
        / provider
        / safe_instance_name(attachment_group)
    ).resolve(strict=False)


def child_attachment_cache_dir(
    *,
    user_id: str,
    attachment_group: str,
    provider: str,
) -> Path:
    return (
        child_instance_workspace_path(user_id)
        / ATTACHMENTS_CACHE_DIR
        / provider
        / safe_instance_name(attachment_group)
    ).resolve(strict=False)


def child_attachment_to_host_path(org_id: str, attachment: Any) -> Any:
    if isinstance(attachment, dict):
        url = str(attachment.get("url") or "").strip()
        if not url or url.startswith(("http://", "https://")):
            return attachment
        mapped = dict(attachment)
        mapped["url"] = child_attachment_to_host_path(org_id, url)
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

    host_path = (HOST_WORKSPACE_ROOT / org_id / relative).resolve(strict=False)
    return str(host_path)


def normalize_outbound_attachments(org_id: str, attachments: list[Any] | None) -> list[Any]:
    return [child_attachment_to_host_path(org_id, item) for item in (attachments or [])]
