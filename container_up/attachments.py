from __future__ import annotations

from urllib.parse import urlparse

def attachment_from_content_url(content: str, prefix: str) -> dict[str, str] | None:
    text = content.strip()
    if not text:
        return None
    if any(ch.isspace() for ch in text):
        return None
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
