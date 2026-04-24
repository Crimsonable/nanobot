"""Minimal container_up service for dynamic nanobot bridge containers."""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4
from venv import logger

import uvicorn
from container_up.attachments import normalize_attachments
from container_up.bridge_state import (
    get_bridge_hub,
    init_bridge_hub,
)
from container_up.im_tools import get_im_manager, get_im_parser, init_im_parser
from container_up.db_store import (
    count_org_records,
    init_db,
    org_record,
    touch_org,
)
from container_up.dispatch import dispatch_parser
from container_up.frontend_config import compose_frontend_org_id
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
    CHILD_BRIDGE_TOKEN,
    CLEANUP_SCAN_INTERVAL,
    IM_PROVIDER,
)
from docker.errors import DockerException
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from pydantic import AliasChoices, BaseModel, Field

cleanup_stop_event = threading.Event()
cleanup_thread: threading.Thread | None = None


class MessageRequest(BaseModel):
    org_id: str
    chat_id: str = "default"
    usr_id: str
    content: str
    attachments: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CancelRequest(BaseModel):
    org_id: str
    chat_id: str = "default"
    usr_id: str


class ShutdownRequest(BaseModel):
    org_id: str


class BridgeOutboundRequest(BaseModel):
    org_id: str
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


async def _dispatch_bridge_outbound_from_ws(
    org_id: str, forwarded: dict[str, Any]
) -> dict[str, Any]:
    try:
        return await dispatch_parser.parse(
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
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception(
            "bridge outbound dispatch failed org_id=%s chat_id=%s",
            org_id,
            forwarded.get("chat_id", ""),
        )
        return {"ok": False, "error": str(exc)}


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
    init_im_parser(
        dispatch_event=_dispatch_subscribe_event,
        main_loop=asyncio.get_running_loop(),
    )
    get_im_manager().start()
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
    try:
        get_im_manager().stop()
    except RuntimeError:
        pass
    await close_dispatch_session()


app = FastAPI(title="container_up", version="0.1.0", lifespan=lifespan)
init_bridge_hub(CHILD_BRIDGE_TOKEN or None)


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
        "im_provider": IM_PROVIDER,
        "im_frontends": get_im_manager().frontend_ids,
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
                await _dispatch_bridge_outbound_from_ws(org_id, forwarded)
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
        event = dict(payload.get("event") or {})
        metadata = dict(event.get("metadata") or {})
        parser = get_im_parser(str(metadata.get("frontend_id") or "") or None)
        prepare_event = getattr(parser, "prepare_inbound_event", None)
        if callable(prepare_event):
            payload = await prepare_event(payload)
        await dispatch_parser.parse(payload)
    except Exception:
        logger.exception("subscribe event dispatch failed: %r", payload)


@app.post("/subscribe/{frontend_id}")
async def subscribe_frontend(frontend_id: str, sub_form: SubForm) -> dict[str, Any]:
    parser = get_im_parser(frontend_id or None)
    try:
        if not parser.supports_subscribe():
            raise HTTPException(
                status_code=404,
                detail="subscribe is not supported for current IM provider",
            )
        print("Received subscribe request:", sub_form)
        response, payload = parser.process_subscribe_form(sub_form)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if payload is not None:
        logger.error("received subscribe event: %r", payload)
        asyncio.create_task(_dispatch_subscribe_event(payload))
    return response


@app.post("/api/message")
async def post_message(payload: MessageRequest) -> dict[str, Any]:
    logger.error(
        "api message start org_id=%s chat_id=%s usr_id=%s attachments_count=%s",
        payload.org_id,
        payload.chat_id,
        payload.usr_id,
        len(payload.attachments),
    )
    try:
        frontend_id = str(payload.metadata.get("frontend_id") or "").strip()
        route_org_id = compose_frontend_org_id(frontend_id, payload.org_id)
        metadata = dict(payload.metadata)
        await run_in_threadpool(
            ensure_org_container,
            route_org_id,
            frontend_id or None,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    await run_in_threadpool(touch_org, route_org_id)
    attachments = normalize_attachments(payload.content, payload.attachments)

    try:
        result = await get_bridge_hub().submit_message(
            org_id=route_org_id,
            chat_id=payload.chat_id,
            usr_id=payload.usr_id,
            content=payload.content,
            attachments=attachments,
            metadata=metadata,
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
            chat_id=payload.chat_id,
            usr_id=payload.usr_id,
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
    return await _dispatch_debug_event(event)


async def _dispatch_debug_event(payload: dict[str, Any]) -> dict[str, Any]:
    parser = get_im_parser(str(payload.get("frontend_id") or "") or None)
    if hasattr(parser, "normalize_subscribe_payload"):
        payload = parser.normalize_subscribe_payload(payload)
    prepare_event = getattr(parser, "prepare_inbound_event", None)
    if callable(prepare_event):
        payload = await prepare_event(payload)
    return await dispatch_parser.parse(payload)


def main() -> None:
    uvicorn.run("container_up.app:app", host=APP_HOST, port=APP_PORT)


if __name__ == "__main__":
    main()
