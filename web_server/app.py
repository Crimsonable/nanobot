from __future__ import annotations

from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from web_server.settings import APP_HOST, APP_PORT, CONTAINER_UP_BASE_URL, DEFAULT_FRONTEND_ID, OUTBOUND_ECHO
from web_server.uvicorn_logging import build_uvicorn_log_config


class InboundRequest(BaseModel):
    frontend_id: str = ""
    user_id: str
    chat_id: str = "default"
    content: str
    attachments: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class OutboundRequest(BaseModel):
    frontend_id: str = ""
    user_id: str = ""
    chat_id: str = ""
    content: str = ""
    attachments: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


app = FastAPI(title="web-server", version="0.1.0")


def _resolve_frontend_id(payload_frontend_id: str) -> str:
    frontend_id = str(payload_frontend_id or DEFAULT_FRONTEND_ID).strip()
    if frontend_id:
        return frontend_id
    raise RuntimeError("frontend_id is required")


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready() -> dict[str, str]:
    return {"status": "ready"}


@app.post("/inbound")
async def inbound(payload: InboundRequest) -> dict[str, Any]:
    frontend_id = _resolve_frontend_id(payload.frontend_id)
    target_url = f"{CONTAINER_UP_BASE_URL}/inbound/{frontend_id}"
    forwarded_payload = {
        "user_id": payload.user_id,
        "chat_id": payload.chat_id,
        "content": payload.content,
        "attachments": list(payload.attachments),
        "metadata": dict(payload.metadata),
        "raw": dict(payload.raw),
    }
    try:
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            response = await client.post(target_url, json=forwarded_payload)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text.strip() or str(exc)
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/outbound")
async def outbound(payload: OutboundRequest) -> dict[str, Any]:
    response = {"status": "accepted"}
    if OUTBOUND_ECHO:
        response["payload"] = payload.model_dump()
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
