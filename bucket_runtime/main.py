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
)
from bucket_runtime.port_allocator import PortAllocator
from bucket_runtime.process_manager import ProcessManager
from bucket_runtime.uvicorn_logging import build_uvicorn_log_config
from bucket_runtime.workspace_manager import WorkspaceManager

manager = ProcessManager(
    workspace_manager=WorkspaceManager(),
    port_allocator=PortAllocator(NANOBOT_PORT_START, NANOBOT_PORT_END),
    idle_ttl=INSTANCE_IDLE_TTL_SECONDS,
)
cleanup_task: asyncio.Task[None] | None = None


class CreateInstanceRequest(BaseModel):
    frontend_id: str
    user_id: str
    instance_id: str
    workspace_path: str


class InboundRequest(BaseModel):
    instance_id: str
    frontend_id: str
    user_id: str
    chat_id: str = "default"
    content: str
    attachments: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class CancelRequest(BaseModel):
    instance_id: str
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


app = FastAPI(title="bucket-runtime", version="0.2.0", lifespan=lifespan)


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
        "max_processes": MAX_PROCESSES_PER_BUCKET,
    }


@app.post("/instances")
async def create_instance(payload: CreateInstanceRequest) -> dict[str, Any]:
    try:
        instance = await manager.create_instance(
            frontend_id=payload.frontend_id,
            user_id=payload.user_id,
            instance_id=payload.instance_id,
            workspace_path=payload.workspace_path,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "bucket_id": BUCKET_ID,
        "instance_id": instance.instance_id,
        "user_id": instance.user_id,
        "status": "online",
        "workspace_path": str(instance.workspace_path),
    }


@app.get("/instances/{instance_id}")
async def get_instance(instance_id: str) -> dict[str, Any]:
    instance = await manager.get_instance(instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="instance not found")
    return {
        "bucket_id": BUCKET_ID,
        "instance_id": instance.instance_id,
        "frontend_id": instance.frontend_id,
        "user_id": instance.user_id,
        "status": "online",
        "workspace_path": str(instance.workspace_path),
        "last_active_at": instance.last_active_at,
    }


@app.delete("/instances/{instance_id}")
async def delete_instance(instance_id: str) -> dict[str, Any]:
    instance = await manager.get_instance(instance_id)
    if instance is None:
        return {"bucket_id": BUCKET_ID, "instance_id": instance_id, "status": "destroyed"}
    await manager.stop_process(instance_id)
    return {
        "bucket_id": BUCKET_ID,
        "instance_id": instance_id,
        "user_id": instance.user_id,
        "status": "destroyed",
    }


@app.post("/inbound")
async def inbound(payload: InboundRequest) -> dict[str, Any]:
    try:
        await manager.forward_inbound(payload.instance_id, payload.model_dump())
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "status": "accepted",
        "bucket_id": BUCKET_ID,
        "frontend_id": payload.frontend_id,
        "user_id": payload.user_id,
        "instance_id": payload.instance_id,
    }


@app.post("/cancel")
async def cancel(payload: CancelRequest) -> dict[str, Any]:
    try:
        await manager.forward_cancel(payload.instance_id, payload.model_dump())
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "status": "accepted",
        "bucket_id": BUCKET_ID,
        "frontend_id": payload.frontend_id,
        "user_id": payload.user_id,
        "instance_id": payload.instance_id,
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
    uvicorn.run(
        "bucket_runtime.main:app",
        host=APP_HOST,
        port=APP_PORT,
        log_config=build_uvicorn_log_config(),
    )


if __name__ == "__main__":
    main()
