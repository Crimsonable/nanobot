from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from container_up.attachment_paths import normalize_outbound_attachments
from container_up.attachments import normalize_attachments
from container_up.binding_repository import BindingRepository
from container_up.bucket_client import BucketClient
from container_up.bucket_manager import BucketManager
from container_up.bucket_scheduler import BucketScheduler
from container_up.http_state import close_dispatch_session, init_dispatch_session
from container_up.im_tools import get_im_manager, get_im_parser, init_im_parser
from container_up.qxt_im_tool import build_im_receive_event
from container_up.settings import (
    APP_HOST,
    APP_PORT,
    BUCKET_MAX_INSTANCES_PER_BUCKET,
    BUCKET_IDLE_SWEEP_INTERVAL_SECONDS,
    BUCKET_WORKSPACE_ROOT,
)
from container_up.workspace_manager import WorkspaceManager

repo = BindingRepository()
bucket_manager = BucketManager()
scheduler = BucketScheduler(
    repo=repo,
    workspace_manager=WorkspaceManager(BUCKET_WORKSPACE_ROOT),
    bucket_manager=bucket_manager,
    bucket_client=BucketClient(),
)
cleanup_task: asyncio.Task[None] | None = None


class InboundRequest(BaseModel):
    user_id: str
    chat_id: str = "default"
    content: str
    attachments: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class MessageRequest(BaseModel):
    frontend_id: str = ""
    chat_id: str = "default"
    usr_id: str
    content: str
    attachments: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CancelRequest(BaseModel):
    chat_id: str = "default"
    usr_id: str
    frontend_id: str = ""


class ReleaseRequest(BaseModel):
    user_id: str
    bucket_id: str = ""
    instance_id: str = ""
    reason: str = ""


class BridgeOutboundRequest(BaseModel):
    frontend_id: str = ""
    to: str
    content: str
    attachments: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class OutboundRequest(BaseModel):
    frontend_id: str
    user_id: str
    chat_id: str
    content: str
    attachments: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class DebugP2PRequest(BaseModel):
    sender_uid: str
    chat_id: str
    content: str
    chat_type: str = "single"
    message_type: str = Field(default="text", alias="type")
    message_id: str = ""
    timestamp: str = ""


class SubForm(BaseModel):
    msgSignature: str
    encrypt: str
    timeStamp: str
    nonce: str


def _resolve_frontend_id(frontend_id: str | None, metadata: dict[str, Any]) -> str:
    configured = str(frontend_id or metadata.get("frontend_id") or "").strip()
    if configured:
        return configured
    frontends = get_im_manager().frontend_ids
    if len(frontends) == 1:
        return frontends[0]
    parser = get_im_manager().parser_for_frontend(None)
    resolved = str(getattr(parser, "frontend_id", "") or "").strip()
    if resolved:
        return resolved
    raise RuntimeError("frontend_id is required when multiple frontends are configured")


async def _route_message(
    *,
    frontend_id: str,
    user_id: str,
    chat_id: str,
    content: str,
    attachments: list[Any],
    metadata: dict[str, Any],
    raw: dict[str, Any],
) -> dict[str, Any]:
    route_metadata = dict(metadata)
    route_metadata.setdefault("frontend_id", frontend_id)
    route_metadata.setdefault("usr_id", user_id)
    runtime = await scheduler.route_inbound(
        frontend_id=frontend_id,
        user_id=user_id,
        payload={
            "frontend_id": frontend_id,
            "user_id": user_id,
            "chat_id": chat_id,
            "content": content,
            "attachments": attachments,
            "metadata": route_metadata,
            "raw": raw,
        },
    )
    return runtime.to_dict()


async def _dispatch_im_event(payload: dict[str, Any]) -> dict[str, Any]:
    event = dict(payload.get("event") or {})
    metadata = dict(event.get("metadata") or {})
    frontend_id = _resolve_frontend_id(None, metadata)
    attachments = list(event.get("attachments") or [])
    if not metadata.get("attachments_materialized"):
        attachments = normalize_attachments(str(event.get("content") or ""), attachments)
    binding = await _route_message(
        frontend_id=frontend_id,
        user_id=str(event.get("usr_id") or "").strip() or "user",
        chat_id=str(event.get("chat_id") or "").strip() or "default",
        content=str(event.get("content") or ""),
        attachments=attachments,
        metadata=metadata,
        raw={"event": event},
    )
    return {
        "ok": True,
        "response": {
            "status": "accepted",
            "frontend_id": frontend_id,
            "user_id": str(event.get("usr_id") or "").strip() or "user",
            "chat_id": str(event.get("chat_id") or "default"),
            "bucket_id": binding["bucket_id"],
            "instance_id": binding["instance_id"],
        },
    }


async def _deliver_outbound_message(
    *,
    chat_id: str,
    content: str,
    metadata: dict[str, Any],
    attachments: list[Any],
) -> dict[str, Any]:
    im_parser = get_im_manager().parser_for_outbound(metadata)
    return await im_parser.post_message_with_retry(
        payload={
            "chat_id": chat_id,
            "content": content,
            "metadata": metadata,
            "attachments": attachments,
        }
    )


async def _forward_outbound_message(packet: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(packet.get("metadata") or {})
    attachments = list(packet.get("attachments") or [])
    if metadata.get("_progress") or metadata.get("_stream_delta") or metadata.get("_stream_end"):
        return {"ok": True, "response": None, "skipped": "non_terminal_event"}

    content = str(packet.get("content") or "")
    if not content and not attachments:
        return {"ok": True, "response": None, "skipped": "empty_content"}

    chat_id = str(packet.get("chat_id") or "")
    response = await _deliver_outbound_message(
        chat_id=chat_id,
        content=content,
        metadata=metadata,
        attachments=attachments,
    )
    return {
        "ok": True,
        "chat_id": chat_id,
        "attachments": attachments,
        "metadata": metadata,
        "response": response,
    }


async def _dispatch_subscribe_event(payload: dict[str, Any]) -> None:
    event = dict(payload.get("event") or {})
    metadata = dict(event.get("metadata") or {})
    parser = get_im_parser(str(metadata.get("frontend_id") or "") or None)
    prepare_event = getattr(parser, "prepare_inbound_event", None)
    if callable(prepare_event):
        payload = await prepare_event(payload)
    await _dispatch_im_event(payload)


async def _cleanup_idle_buckets() -> None:
    while True:
        await asyncio.sleep(BUCKET_IDLE_SWEEP_INTERVAL_SECONDS)
        for bucket in repo.list_idle_buckets_ready_for_scale_down():
            try:
                await bucket_manager.scale_bucket_to_zero(bucket)
                repo.touch_bucket(str(bucket["bucket_id"]), status="idle")
            except Exception:
                continue


@asynccontextmanager
async def lifespan(_: FastAPI):
    global cleanup_task
    repo.init_db()
    init_dispatch_session()
    init_im_parser(
        dispatch_event=_dispatch_subscribe_event,
        main_loop=asyncio.get_running_loop(),
    )
    get_im_manager().start()
    cleanup_task = asyncio.create_task(_cleanup_idle_buckets())
    yield
    try:
        get_im_manager().stop()
    except RuntimeError:
        pass
    if cleanup_task is not None:
        cleanup_task.cancel()
        await asyncio.gather(cleanup_task, return_exceptions=True)
    await close_dispatch_session()


app = FastAPI(title="container-up", version="0.3.0", lifespan=lifespan)


@app.get("/health/live")
def health_live() -> dict[str, Any]:
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready() -> dict[str, Any]:
    return {"status": "ready", "bucket_capacity": BUCKET_MAX_INSTANCES_PER_BUCKET}


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "bucket_capacity": BUCKET_MAX_INSTANCES_PER_BUCKET,
        "bucket_count": len(repo.list_buckets()),
        "im_frontends": get_im_manager().frontend_ids,
    }


@app.get("/binding/{frontend_id}/{user_id}")
def get_binding(frontend_id: str, user_id: str) -> dict[str, Any]:
    binding = repo.get(frontend_id, user_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="binding not found")
    return binding


@app.post("/inbound/{frontend_id}")
async def inbound(frontend_id: str, payload: InboundRequest) -> dict[str, Any]:
    try:
        binding = await _route_message(
            frontend_id=frontend_id,
            user_id=payload.user_id,
            chat_id=payload.chat_id,
            content=payload.content,
            attachments=normalize_attachments(payload.content, payload.attachments),
            metadata=payload.metadata,
            raw=payload.raw,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "status": "accepted",
        "frontend_id": frontend_id,
        "user_id": payload.user_id,
        "bucket_id": binding["bucket_id"],
        "instance_id": binding["instance_id"],
    }


@app.post("/api/message")
async def post_message(payload: MessageRequest) -> dict[str, Any]:
    try:
        frontend_id = _resolve_frontend_id(payload.frontend_id, payload.metadata)
        binding = await _route_message(
            frontend_id=frontend_id,
            user_id=payload.usr_id,
            chat_id=payload.chat_id,
            content=payload.content,
            attachments=normalize_attachments(payload.content, payload.attachments),
            metadata=payload.metadata,
            raw={},
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "status": "accepted",
        "frontend_id": frontend_id,
        "user_id": payload.usr_id,
        "chat_id": payload.chat_id,
        "bucket_id": binding["bucket_id"],
        "instance_id": binding["instance_id"],
    }


@app.post("/api/cancel")
async def post_cancel(payload: CancelRequest) -> dict[str, Any]:
    frontend_id = str(payload.frontend_id or "").strip()
    if not frontend_id:
        instance = repo.get_user_instance(payload.usr_id)
        if instance is not None:
            frontend_id = str(instance.get("frontend_id") or "").strip()
    if not frontend_id:
        try:
            frontend_id = _resolve_frontend_id(None, {})
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        runtime = await scheduler.route_cancel(
            frontend_id=frontend_id,
            user_id=payload.usr_id,
            payload={
                "frontend_id": frontend_id,
                "user_id": payload.usr_id,
                "chat_id": payload.chat_id,
                "metadata": {"frontend_id": frontend_id, "usr_id": payload.usr_id},
                "raw": {},
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "status": "accepted",
        "frontend_id": frontend_id,
        "user_id": payload.usr_id,
        "chat_id": payload.chat_id,
        "bucket_id": runtime.bucket_id if runtime is not None else None,
        "instance_id": runtime.instance_id if runtime is not None else None,
    }


@app.post("/internal/runtime/release")
async def runtime_release(payload: ReleaseRequest) -> dict[str, Any]:
    instance = scheduler.sync_runtime_release(
        user_id=payload.user_id,
        bucket_id=payload.bucket_id or None,
        instance_id=payload.instance_id or None,
    )
    return {
        "status": "accepted",
        "user_id": payload.user_id,
        "bucket_id": payload.bucket_id or None,
        "instance_id": payload.instance_id or None,
        "instance_status": None if instance is None else instance.get("status"),
        "reason": payload.reason,
    }


@app.post("/outbound")
async def outbound(payload: OutboundRequest) -> dict[str, Any]:
    metadata = dict(payload.metadata)
    metadata.setdefault("frontend_id", payload.frontend_id)
    metadata.setdefault("usr_id", payload.user_id)
    return await _forward_outbound_message(
        {
            "type": "outbound_message",
            "chat_id": payload.chat_id,
            "content": payload.content,
            "metadata": metadata,
            "attachments": list(payload.attachments),
        }
    )


@app.post("/api/bridge/outbound")
async def post_bridge_outbound(payload: BridgeOutboundRequest) -> dict[str, Any]:
    metadata = dict(payload.metadata)
    frontend_id = str(payload.frontend_id or metadata.get("frontend_id") or "").strip() or None
    return await _forward_outbound_message(
        {
            "type": "outbound_message",
            "chat_id": payload.to,
            "content": payload.content,
            "metadata": metadata,
            "attachments": normalize_outbound_attachments(
                list(payload.attachments),
                frontend_id=frontend_id,
            ),
        }
    )


@app.post("/subscribe/{frontend_id}")
async def subscribe_frontend(frontend_id: str, sub_form: SubForm) -> dict[str, Any]:
    parser = get_im_parser(frontend_id or None)
    try:
        if not parser.supports_subscribe():
            raise HTTPException(status_code=404, detail="subscribe is not supported")
        response, payload = parser.process_subscribe_form(sub_form)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload is not None:
        asyncio.create_task(_dispatch_subscribe_event(payload))
    return response


@app.post("/api/debug/p2p")
async def debug_p2p(payload: DebugP2PRequest) -> dict[str, Any]:
    event = {
        "event_type": "p2p_chat_receive_msg",
        "timestamp": payload.timestamp or str(uuid4().int),
        "event": {
            "sender_uid": payload.sender_uid,
            "message": {
                "chat_id": payload.chat_id,
                "content": payload.content,
                "chat_type": payload.chat_type,
                "type": payload.message_type,
                "message_id": payload.message_id or f"debug-{uuid4().hex}",
            },
        },
    }
    parser = get_im_parser(None)
    if hasattr(parser, "normalize_subscribe_payload"):
        standardized = parser.normalize_subscribe_payload(event)
    else:
        standardized = build_im_receive_event(
            chat_id=payload.chat_id,
            usr_id=payload.sender_uid,
            content=payload.content,
            attachments=[],
            metadata={"frontend_id": getattr(parser, "frontend_id", "default")},
        )
    prepare_event = getattr(parser, "prepare_inbound_event", None)
    if callable(prepare_event):
        standardized = await prepare_event(standardized)
    return await _dispatch_im_event(standardized)


@app.post("/debug/message")
async def debug_message(
    usr_id: str,
    chat_id: str = "default",
    content: str = "Hello from debug",
) -> dict[str, Any]:
    event = build_im_receive_event(
        chat_id=chat_id,
        usr_id=usr_id,
        content=content,
        attachments=[],
        metadata={"frontend_id": "qxt-main", "provider": "qxt"},
    )
    parser = get_im_parser("qxt-main")
    prepare_event = getattr(parser, "prepare_inbound_event", None)
    if callable(prepare_event):
        event = await prepare_event(event)
    return await _dispatch_im_event(event)


def main() -> None:
    uvicorn.run("container_up.app:app", host=APP_HOST, port=APP_PORT)


if __name__ == "__main__":
    main()
