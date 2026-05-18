from __future__ import annotations

import asyncio
from uuid import uuid4
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from web_server.settings import (
    APP_HOST,
    APP_PORT,
    CONTAINER_UP_BASE_URL,
    DEFAULT_FRONTEND_ID,
    OUTBOUND_ECHO,
)
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


class BatchInboundTestRequest(BaseModel):
    frontend_id: str = ""
    n: int = Field(..., ge=1, le=1000)
    content: str = "ping"


app = FastAPI(title="web-server", version="0.1.0")


def _resolve_frontend_id(payload_frontend_id: str) -> str:
    frontend_id = str(payload_frontend_id or DEFAULT_FRONTEND_ID).strip()
    if frontend_id:
        return frontend_id
    raise RuntimeError("frontend_id is required")


async def _forward_inbound(frontend_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    target_url = f"{CONTAINER_UP_BASE_URL}/inbound/{frontend_id}"
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
async def inbound(payload: InboundRequest) -> dict[str, Any]:
    frontend_id = _resolve_frontend_id(payload.frontend_id)
    forwarded_payload = {
        "user_id": payload.user_id,
        "chat_id": payload.chat_id,
        "content": payload.content,
        "attachments": list(payload.attachments),
        "metadata": dict(payload.metadata),
        "raw": dict(payload.raw),
    }
    return await _forward_inbound(frontend_id, forwarded_payload)


@app.post("/test/create-instances")
async def create_instances_for_test(payload: BatchInboundTestRequest) -> dict[str, Any]:
    frontend_id = _resolve_frontend_id(payload.frontend_id)
    batch_id = uuid4().hex[:8]
    requests: list[dict[str, str]] = []
    tasks = []
    for index in range(payload.n):
        user_id = f"test-user-{batch_id}-{index + 1}"
        chat_id = f"test-chat-{batch_id}-{index + 1}"
        forwarded_payload = {
            "user_id": user_id,
            "chat_id": chat_id,
            "content": payload.content,
            "attachments": [],
            "metadata": {
                "test_batch": True,
                "batch_id": batch_id,
                "batch_index": index + 1,
            },
            "raw": {},
        }
        requests.append({"user_id": user_id, "chat_id": chat_id})
        tasks.append(_forward_inbound(frontend_id, forwarded_payload))
    responses = await asyncio.gather(*tasks)
    return {
        "status": "accepted",
        "frontend_id": frontend_id,
        "batch_id": batch_id,
        "count": payload.n,
        "requests": requests,
        "responses": responses,
    }


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
