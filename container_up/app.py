"""Minimal container_up service for dynamic nanobot bridge containers."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from typing import Any
from venv import logger

import uvicorn
from container_up.attachments import normalize_attachments
from container_up.bridge_state import (
    get_bridge_hub,
    init_bridge_hub,
)
from container_up.im_tools import get_im_parser, init_im_parser
from container_up.db_store import (
    count_org_records,
    init_db,
    org_record,
    touch_org,
)
from container_up.dispatch import dispatch_parser
from container_up.http_state import close_dispatch_session, init_dispatch_session
from container_up.router_service import (
    cleanup_idle_orgs,
    docker_client,
    ensure_child_network,
    ensure_org_container,
    get_container,
    shutdown_all_org_containers,
    shutdown_org_container,
    sync_existing_orgs,
)
from container_up.settings import (
    APP_HOST,
    APP_PORT,
    APP_SECRET,
    CHILD_BRIDGE_TOKEN,
    CLEANUP_SCAN_INTERVAL,
)
from docker.errors import DockerException
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from pydantic import AliasChoices, BaseModel, Field

cleanup_stop_event = threading.Event()
cleanup_thread: threading.Thread | None = None


class MessageRequest(BaseModel):
    org_id: str = Field(validation_alias=AliasChoices("org_id", "organization_id"))
    conversation_id: str = Field(
        default="default",
        validation_alias=AliasChoices("conversation_id", "session_id"),
    )
    user_id: str = Field(validation_alias=AliasChoices("user_id", "usr_id"))
    content: str
    attachments: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CancelRequest(BaseModel):
    org_id: str = Field(validation_alias=AliasChoices("org_id", "organization_id"))
    conversation_id: str = Field(
        default="default",
        validation_alias=AliasChoices("conversation_id", "session_id"),
    )
    user_id: str = Field(validation_alias=AliasChoices("user_id", "usr_id"))


class ShutdownRequest(BaseModel):
    org_id: str = Field(validation_alias=AliasChoices("org_id", "organization_id"))


class BridgeOutboundRequest(BaseModel):
    org_id: str = Field(validation_alias=AliasChoices("org_id", "organization_id"))
    to: str
    content: str
    attachments: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DebugP2PRequest(BaseModel):
    sender_uid: str
    chat_id: str
    content: str
    chat_type: str = "single"
    message_type: str = Field(
        default="text",
        validation_alias=AliasChoices("message_type", "type"),
    )
    message_id: str = ""
    timestamp: str = ""


def cleanup_loop() -> None:
    while not cleanup_stop_event.wait(CLEANUP_SCAN_INTERVAL):
        try:
            cleanup_idle_orgs()
        except Exception:
            continue


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    ensure_child_network()
    docker_client.ping()
    sync_existing_orgs()
    init_dispatch_session()
    cleanup_stop_event.clear()
    global cleanup_thread
    cleanup_thread = threading.Thread(
        target=cleanup_loop, name="container-up-cleanup", daemon=True
    )
    cleanup_thread.start()
    yield
    cleanup_stop_event.set()
    if cleanup_thread is not None:
        cleanup_thread.join(timeout=5)
    await close_dispatch_session()


app = FastAPI(title="container_up", version="0.1.0", lifespan=lifespan)
init_bridge_hub(CHILD_BRIDGE_TOKEN or None)
init_im_parser()


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    try:
        docker_client.ping()
        docker_ok = True
    except DockerException:
        docker_ok = False
    bridge_hub = get_bridge_hub()
    return {
        "status": "ok" if docker_ok else "degraded",
        "docker": docker_ok,
        "tracked_orgs": count_org_records(),
        "connected_bridge_orgs": bridge_hub.child_count,
    }


@app.websocket("/ws/bridge")
async def bridge_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    bridge_hub = get_bridge_hub()
    org_id: str | None = None
    try:
        raw = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        org_id = await bridge_hub.register_child(websocket, raw)
        if org_id is None:
            return

        while True:
            packet = await websocket.receive_json()
            forwarded = await bridge_hub.handle_child_packet(org_id, packet)
            if str(forwarded.get("type") or "") == "outbound_message":
                await dispatch_parser.parse(
                    {
                        "event_type": "bridge_outbound_message",
                        "org_id": org_id,
                        "event": {
                            "to": forwarded.get("chat_id", ""),
                            "content": forwarded.get("content", ""),
                            "metadata": forwarded.get("metadata", {}),
                            "attachments": forwarded.get("attachments", []),
                        },
                    }
                )
    except WebSocketDisconnect:
        pass
    except asyncio.TimeoutError:
        await websocket.close(code=4001, reason="register timeout")
    finally:
        if org_id is not None:
            bridge_hub.unregister_child(org_id, websocket)


@app.get("/api/org/{org_id}")
def get_org(org_id: str) -> dict[str, Any]:
    record = org_record(org_id)
    if record is None:
        raise HTTPException(status_code=404, detail="org not found")
    container = get_container(record["container_name"])
    child = get_bridge_hub().child_for_org(org_id)
    return {
        "org_id": org_id,
        "record": record,
        "container_status": (
            getattr(container, "status", "missing") if container else "missing"
        ),
        "bridge_connected": child is not None,
    }


class SubForm(BaseModel):
    msgSignature: str
    encrypt: str
    timeStamp: str
    nonce: str


async def _dispatch_subscribe_event(payload: dict[str, Any]) -> None:
    try:
        await dispatch_parser.parse(payload)
    except Exception:
        logger.exception("subscribe event dispatch failed: %r", payload)


@app.post("/subscribe")
async def subscribe(sub_form: SubForm) -> dict[str, Any]:
    if not APP_SECRET:
        raise HTTPException(status_code=500, detail="app_secret is not configured")

    parser = get_im_parser()
    try:
        decrypted = parser.decrypt(
            signature=sub_form.msgSignature,
            timeStamp=sub_form.timeStamp,
            nonce=sub_form.nonce,
            encrypt=sub_form.encrypt,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not decrypted:
        raise HTTPException(status_code=400, detail="empty decrypted payload")

    try:
        payload = json.loads(decrypted)
        logger.error("received subscribe event: %r", payload)
    except json.JSONDecodeError:
        logger.error("failed to decode decrypted payload as json: %r", decrypted)
        return {"error": "invalid payload"}

    event_type = str(payload.get("event_type") or "")
    if event_type == "check_url":
        return parser.encrypt(
            text="success",
        )

    asyncio.create_task(_dispatch_subscribe_event(payload))
    return parser.encrypt(
        text="success",
    )


@app.post("/api/message")
async def post_message(payload: MessageRequest) -> dict[str, Any]:
    logger.error(
        "api message start org_id=%s conversation_id=%s user_id=%s attachments_count=%s",
        payload.org_id,
        payload.conversation_id,
        payload.user_id,
        len(payload.attachments),
    )
    try:
        await run_in_threadpool(ensure_org_container, payload.org_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    await run_in_threadpool(touch_org, payload.org_id)
    attachments = normalize_attachments(payload.content, payload.attachments)

    try:
        result = await get_bridge_hub().submit_message(
            org_id=payload.org_id,
            conversation_id=payload.conversation_id,
            user_id=payload.user_id,
            content=payload.content,
            attachments=attachments,
            metadata=payload.metadata,
        )
        return result
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/bridge/outbound")
async def post_bridge_outbound(
    payload: BridgeOutboundRequest,
    x_bridge_token: str | None = Header(default=None),
) -> dict[str, Any]:
    if CHILD_BRIDGE_TOKEN and x_bridge_token != CHILD_BRIDGE_TOKEN:
        raise HTTPException(status_code=403, detail="invalid bridge token")
    try:
        return await dispatch_parser.parse(
            {
                "event_type": "bridge_outbound_message",
                "org_id": payload.org_id,
                "event": payload.model_dump(),
            }
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/cancel")
async def post_cancel(payload: CancelRequest) -> dict[str, Any]:
    record = await run_in_threadpool(org_record, payload.org_id)
    if record is None:
        raise HTTPException(status_code=404, detail="org not found")
    await run_in_threadpool(touch_org, payload.org_id)
    try:
        return await get_bridge_hub().submit_cancel(
            org_id=payload.org_id,
            conversation_id=payload.conversation_id,
            user_id=payload.user_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/shutdown")
async def post_shutdown(payload: ShutdownRequest) -> dict[str, Any]:
    if payload.org_id == "ALL":
        results = await run_in_threadpool(shutdown_all_org_containers)
        return {
            "scope": "all",
            "count": len(results),
            "results": results,
        }

    result = await run_in_threadpool(shutdown_org_container, payload.org_id)
    return {
        "scope": "single",
        "result": result,
    }


@app.post("/api/debug/p2p")
async def debug_p2p(payload: DebugP2PRequest) -> dict[str, Any]:
    event = {
        "event_type": "p2p_chat_receive_msg",
        "timestamp": payload.timestamp or str(int(time.time())),
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
    return await dispatch_parser.parse(event)


def main() -> None:
    uvicorn.run("container_up.app:app", host=APP_HOST, port=APP_PORT)


if __name__ == "__main__":
    main()
