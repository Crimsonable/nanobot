from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field, StrictBool, field_validator

from agent_backend.models import StrictModel


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
