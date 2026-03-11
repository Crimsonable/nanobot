"""Minimal FastAPI app for bridge_service."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from bridge_service.core import BridgeCore


class MessageRequest(BaseModel):
    conversation_id: str
    user_id: str
    content: str
    tenant_id: str = "default"
    request_id: str | None = None
    attachments: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = 60.0


class CancelRequest(BaseModel):
    conversation_id: str
    user_id: str = "remote-control"
    tenant_id: str = "default"
    request_id: str = ""


def create_app(*, token: str | None = None) -> FastAPI:
    app = FastAPI(title="nanobot bridge service", version="0.1.0")
    app.state.bridge = BridgeCore(token=token)

    def get_bridge() -> BridgeCore:
        return app.state.bridge

    async def verify_http_token(
        authorization: str | None = Header(default=None),
        x_bridge_token: str | None = Header(default=None),
        bridge: BridgeCore = Depends(get_bridge),
    ) -> None:
        if not bridge.token:
            return
        provided = x_bridge_token
        if authorization and authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
        if provided != bridge.token:
            raise HTTPException(status_code=401, detail="invalid token")

    @app.get("/healthz")
    async def healthz(bridge: BridgeCore = Depends(get_bridge)) -> dict[str, Any]:
        return {"status": "ok", "bots_connected": bridge.bot_count}

    @app.post("/api/messages", dependencies=[Depends(verify_http_token)])
    async def post_message(
        payload: MessageRequest,
        bridge: BridgeCore = Depends(get_bridge),
    ) -> dict[str, Any]:
        try:
            return await bridge.submit_message(
                conversation_id=payload.conversation_id,
                user_id=payload.user_id,
                tenant_id=payload.tenant_id,
                content=payload.content,
                attachments=payload.attachments,
                metadata=payload.metadata,
                request_id=payload.request_id,
                timeout=payload.timeout_seconds,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="bridge response timeout") from exc

    @app.post("/api/cancel", dependencies=[Depends(verify_http_token)])
    async def post_cancel(
        payload: CancelRequest,
        bridge: BridgeCore = Depends(get_bridge),
    ) -> dict[str, Any]:
        try:
            return await bridge.submit_cancel(
                conversation_id=payload.conversation_id,
                user_id=payload.user_id,
                tenant_id=payload.tenant_id,
                request_id=payload.request_id,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.websocket("/")
    async def bot_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        bridge = get_bridge()
        authenticated = False
        try:
            if bridge.token:
                raw = await asyncio.wait_for(websocket.receive_json(), timeout=5)
                authenticated = await bridge.authenticate_ws(websocket, raw)
                if not authenticated:
                    return
            else:
                authenticated = True

            bridge.register_bot(websocket)
            while True:
                packet = await websocket.receive_json()
                await bridge.handle_bot_packet(packet)
        except WebSocketDisconnect:
            pass
        except asyncio.TimeoutError:
            await websocket.close(code=4001, reason="auth timeout")
        finally:
            if authenticated:
                bridge.unregister_bot(websocket)

    return app
