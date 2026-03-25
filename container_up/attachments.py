from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

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
