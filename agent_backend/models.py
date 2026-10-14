from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator


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
    def identity_id(self) -> str:
        return digest(self.model_dump())


class Attachment(StrictModel):
    attachment_id: str = Field(default_factory=lambda: uid("att_"))
    url: str = ""
    path: str = ""
    requires_auth: bool = False
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
    chat_id: str = "default"
    identity: Identity
    profile_id: str
    content: str = Field(max_length=200_000)
    attachments: list[Attachment] = Field(default_factory=list, max_length=32)
    deadline_at: float
    session_epoch: str = ""
    previous_turns: list[dict[str, Any]] = Field(default_factory=list)
    _legacy_session_key: str | None = PrivateAttr(default=None)

    @property
    def session_key(self) -> str:
        """Internal SQLite key, never a separate ID in the protocol or logs."""
        return self._legacy_session_key or digest([self.identity.identity_id, self.chat_id])

    @property
    def has_chat_id(self) -> bool:
        return self._legacy_session_key is None

    @classmethod
    def from_record(cls, value: dict) -> Submit:
        """Read pre-upgrade runs whose original chat_id was stored only in CU."""
        payload = dict(value)
        legacy_key = payload.pop("conversation_id", None)
        submit = cls.model_validate(payload)
        if legacy_key is not None:
            if "chat_id" in payload:
                if legacy_key != submit.session_key:
                    raise ValueError("persisted chat key mismatch")
            else:
                # Hashes cannot be reversed. Retain the old lookup key for recovery,
                # and omit chat_id from legacy logs instead of inventing an identity.
                submit._legacy_session_key = legacy_key
        return submit


class Usage(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)


class OpenCodeEvent(StrictModel):
    type: Literal["opencode.event"]
    event_id: str = Field(min_length=1, max_length=100)
    directory: str = Field(min_length=1)
    session_id: str = ""
    message_id: str = ""
    input_message_id: str = ""
    request_id: str = ""
    event: dict[str, Any]


class OpenCodeEventChunk(StrictModel):
    event_id: str = Field(min_length=1, max_length=100)
    index: int = Field(ge=0)
    count: int = Field(ge=1)
    data: str


class PluginEvent(StrictModel):
    type: Literal["response.delta", "tool.progress", "result", "error"]
    request_id: str
    session_id: str
    input_message_id: str
    directory: str = Field(min_length=1)
    revision: int = Field(ge=1)
    content: str = Field(default="", max_length=200_000)
    messages: list[dict[str, Any]] = Field(default_factory=list, max_length=4096)
    error: str | None = Field(default=None, max_length=1000)
    call_id: str | None = None
    tool: str | None = None
    status: str | None = None


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
