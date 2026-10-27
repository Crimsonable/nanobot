from __future__ import annotations

from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from web_server.settings import (
    APP_HOST,
    APP_PORT,
    GATEWAY_BASE_URL,
    OUTBOUND_ECHO,
)
from web_server.uvicorn_logging import build_uvicorn_log_config


class InboundRequest(BaseModel):
    """Lenient front-door contract mirroring agent_backend.ingress.Inbound."""

    model_config = ConfigDict(extra="ignore")
    frontend_id: str = Field(min_length=1)
    user_id: str
    chat_id: str = "default"
    content: str
    attachments: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("chat_id", mode="before")
    @classmethod
    def default_chat_id(cls, value: Any) -> str:
        if value is None:
            return "default"
        text = str(value)
        return text if text.strip() else "default"


class OutboundRequest(BaseModel):
    frontend_id: str = ""
    user_id: str = ""
    chat_id: str = ""
    content: str = ""
    attachments: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


app = FastAPI(title="web-server", version="0.1.0")


async def _forward_inbound(frontend_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    target_url = f"{GATEWAY_BASE_URL}/inbound/{frontend_id}"
    try:
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            response = await client.post(target_url, json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text.strip() or str(exc)
        raise HTTPException(
            status_code=exc.response.status_code, detail=detail
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready() -> dict[str, str]:
    return {"status": "ready"}


@app.post("/inbound")
async def inbound(payload: InboundRequest, request: Request) -> dict[str, Any]:
    frontend_id = payload.frontend_id
    forwarded_payload = {
        "user_id": payload.user_id,
        "chat_id": payload.chat_id,
        "content": payload.content,
        "attachments": list(payload.attachments),
        "metadata": dict(payload.metadata),
    }
    return await _forward_inbound(frontend_id, forwarded_payload)


@app.post("/outbound")
async def outbound(payload: OutboundRequest) -> dict[str, Any]:
    response = {"status": "accepted"}
    if OUTBOUND_ECHO:
        response["payload"] = payload.model_dump()
    print(f"Outbound message received: {payload.json()}")
    return response


def main() -> None:
    uvicorn.run(
        "web_server.app:app",
        host=APP_HOST,
        port=APP_PORT,
        log_config=build_uvicorn_log_config(),
    )


if __name__ == "__main__":
    main()
