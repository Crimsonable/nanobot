from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from bucket_runtime.config import (
    APP_HOST,
    APP_PORT,
    BUCKET_ID,
    INSTANCE_EVICT_INTERVAL_SECONDS,
    INSTANCE_IDLE_TTL_SECONDS,
    MAX_PROCESSES_PER_BUCKET,
    NANOBOT_PORT_END,
    NANOBOT_PORT_START,
    TEMPLATES_ROOT,
    WORKSPACE_ROOT,
)
from bucket_runtime.port_allocator import PortAllocator
from bucket_runtime.process_manager import ProcessManager
from bucket_runtime.workspace_manager import WorkspaceManager

manager = ProcessManager(
    workspace_manager=WorkspaceManager(WORKSPACE_ROOT, TEMPLATES_ROOT),
    port_allocator=PortAllocator(NANOBOT_PORT_START, NANOBOT_PORT_END),
    idle_ttl=INSTANCE_IDLE_TTL_SECONDS,
)
cleanup_task: asyncio.Task[None] | None = None


class InboundRequest(BaseModel):
    frontend_id: str
    user_id: str
    chat_id: str = "default"
    content: str
    attachments: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class CancelRequest(BaseModel):
    frontend_id: str
    user_id: str
    chat_id: str = "default"
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(INSTANCE_EVICT_INTERVAL_SECONDS)
        await manager.reap_idle_processes()


@asynccontextmanager
async def lifespan(_: FastAPI):
    global cleanup_task
    cleanup_task = asyncio.create_task(_cleanup_loop())
    yield
    if cleanup_task is not None:
        cleanup_task.cancel()
        await asyncio.gather(cleanup_task, return_exceptions=True)
    await manager.close()


app = FastAPI(title="bucket-runtime", version="0.1.0", lifespan=lifespan)


@app.get("/health/live")
def health_live() -> dict[str, Any]:
    return {"status": "ok", "bucket_id": BUCKET_ID}


@app.get("/health/ready")
def health_ready() -> dict[str, Any]:
    return {"status": "ready", "bucket_id": BUCKET_ID}


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "bucket_id": BUCKET_ID,
        "running_processes": len(manager.status()),
    }


@app.post("/inbound")
async def inbound(payload: InboundRequest) -> dict[str, Any]:
    try:
        await manager.forward_inbound(
            payload.frontend_id,
            payload.user_id,
            payload.model_dump(),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "status": "accepted",
        "bucket_id": BUCKET_ID,
        "frontend_id": payload.frontend_id,
        "user_id": payload.user_id,
    }


@app.post("/cancel")
async def cancel(payload: CancelRequest) -> dict[str, Any]:
    try:
        await manager.forward_cancel(
            payload.frontend_id,
            payload.user_id,
            payload.model_dump(),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "status": "accepted",
        "bucket_id": BUCKET_ID,
        "frontend_id": payload.frontend_id,
        "user_id": payload.user_id,
    }


@app.get("/bucket/status")
def bucket_status() -> dict[str, Any]:
    users = manager.status()
    return {
        "bucket_id": BUCKET_ID,
        "running_processes": len(users),
        "max_processes": MAX_PROCESSES_PER_BUCKET,
        "users": users,
    }


def main() -> None:
    uvicorn.run("bucket_runtime.main:app", host=APP_HOST, port=APP_PORT)


if __name__ == "__main__":
    main()
