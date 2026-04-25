from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from container_up.attachment_paths import (
    child_attachment_cache_dir,
    host_attachment_cache_dir,
)
from container_up.settings import ATTACHMENT_URL_PREFIX


def attachment_from_content_url(content: str) -> dict[str, str] | None:
    text = content.strip()
    if not text:
        return None
    if any(ch.isspace() for ch in text):
        return None
    if not ATTACHMENT_URL_PREFIX:
        return None
    if not text.startswith(ATTACHMENT_URL_PREFIX):
        return None

    parsed = urlparse(text)
    if not parsed.netloc:
        return None

    return {
        "url": text,
        "source": "content_url",
    }


def normalize_attachments(content: str, attachments: list[Any] | None) -> list[Any]:
    normalized = list(attachments or [])
    auto_attachment = attachment_from_content_url(content)
    if auto_attachment and not any(
        (isinstance(item, dict) and item.get("url") == auto_attachment["url"])
        or item == auto_attachment["url"]
        for item in normalized
    ):
        normalized.append(auto_attachment)
    return normalized


def sanitize_attachment_filename(filename: str, fallback: str = "attachment.bin") -> str:
    text = "".join(ch if ch.isalnum() or ch in {".", "-", "_"} else "_" for ch in filename)
    text = text.strip("._") or fallback
    if "." not in text and "." in fallback:
        text = f"{text}{Path(fallback).suffix}"
    return text


def persist_attachment_bytes(
    *,
    user_id: str,
    data: bytes,
    filename: str,
    provider: str,
    attachment_group: str,
    frontend_id: str | None = None,
) -> str:
    if not data:
        raise RuntimeError("empty attachment data")

    safe_filename = sanitize_attachment_filename(filename)
    host_dir = host_attachment_cache_dir(
        user_id=user_id,
        attachment_group=attachment_group,
        provider=provider,
        frontend_id=frontend_id,
    )
    child_dir = child_attachment_cache_dir(
        user_id=user_id,
        attachment_group=attachment_group,
        provider=provider,
        frontend_id=frontend_id,
    )
    host_dir.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha1(data).hexdigest()[:12]
    file_name = f"{int(time.time())}-{digest}-{safe_filename}"
    host_target = host_dir / file_name
    host_target.write_bytes(data)
    return str((child_dir / file_name).resolve(strict=False))
