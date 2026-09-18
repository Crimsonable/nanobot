from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

ATTACHMENT_URL_PREFIX = ""


def attachment_from_content_url(content: str, prefix: str | None = None) -> dict[str, str] | None:
    text = content.strip()
    if not text:
        return None
    if any(ch.isspace() for ch in text):
        return None
    prefix = ATTACHMENT_URL_PREFIX if prefix is None else prefix
    if not prefix:
        return None
    if not text.startswith(prefix):
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
