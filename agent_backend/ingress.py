from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import ConfigDict, Field, StrictBool, field_validator

from agent_backend.models import Attachment, StrictModel


class Inbound(StrictModel):
    """Lenient inbound contract.

    Unknown fields are ignored and a missing or blank `chat_id` falls back to
    `"default"` so legacy callers that omit or pad the field keep working.
    """

    model_config = ConfigDict(extra="ignore")
    user_id: str
    chat_id: str = "default"
    content: str = Field(max_length=200_000)
    attachments: list[str | dict[str, Any]] = Field(default_factory=list, max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None
    # Omit false on the internal wire to preserve pre-stream request fingerprints.
    stream: StrictBool = Field(default=False, exclude_if=lambda value: not value)

    @field_validator("chat_id", mode="before")
    @classmethod
    def default_chat_id(cls, value: Any) -> str:
        if value is None:
            return "default"
        text = str(value)
        return text if text.strip() else "default"

    def attachment_references(self) -> list[Attachment]:
        return [_attachment_reference(value) for value in self.attachments]


def _attachment_reference(value: str | dict) -> Attachment:
    raw = {"url": value} if isinstance(value, str) else value
    reference = str(raw.get("url") or raw.get("path") or "")
    if not reference:
        raise ValueError("attachment requires a URL or file path")
    parsed = urlparse(reference)
    is_url = bool(parsed.scheme) and reference[1:2] != ":"
    filename = raw.get("filename") or Path(parsed.path.replace("\\", "/")).name or "attachment.bin"
    return Attachment(url=reference if is_url else "", path="" if is_url else reference,
                      filename=filename, requires_auth=bool(raw.get("requires_auth", False)),
                      content_type=raw.get("content_type") or mimetypes.guess_type(filename)[0]
                      or "application/octet-stream")
