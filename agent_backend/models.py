from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def uid(prefix: str = "") -> str:
    return prefix + uuid4().hex


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


class Identity(StrictModel):
    frontend_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=500)

    @property
    def key(self) -> str:
        return digest(self.model_dump())


class Attachment(StrictModel):
    attachment_id: str = Field(default_factory=lambda: uid("att_"))
    url: str = ""
    filename: str = "attachment.bin"
    content_type: str = "application/octet-stream"
    size: int = Field(default=0, ge=0)
    sha256: str = ""
    storage: str = ""
    object_key: str = ""

    @field_validator("filename")
    @classmethod
    def filename_only(cls, value: str) -> str:
        if not value or value in {".", ".."} or any(c in value for c in "/\\\x00\r\n"):
            raise ValueError("filename must be a single file name")
        return value


class Submit(StrictModel):
    request_id: str = Field(min_length=1, max_length=100)
    conversation_id: str
    identity: Identity
    profile_id: str
    content: str = Field(max_length=200_000)
    attachments: list[Attachment] = Field(default_factory=list, max_length=32)
    deadline_at: float


class Usage(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)


class Result(StrictModel):
    request_id: str
    status: Literal["completed", "failed", "cancelled"]
    content: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    error: str | None = None
    partial: bool = False
    usage: Usage | None = None


class Envelope(StrictModel):
    protocol: Literal["agent-bucket/1"] = "agent-bucket/1"
    event_id: str = Field(default_factory=lambda: uid("evt_"))
    type: str
    bucket_id: str
    seq: int = Field(default=0, ge=0)
    correlation_id: str = ""
    request_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


TERMINAL = {"completed", "failed", "cancelled"}
