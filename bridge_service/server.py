"""Standalone bridge service for nanobot's built-in bridge channel."""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import websockets

from bridge_service.protocol import (
    PROTOCOL_VERSION,
    decode_packet,
    encode_packet,
    make_request_id,
    make_session_key,
)


class BridgeService:
    """Route client requests to nanobot's bridge channel over WebSocket."""

    def __init__(
        self,
        client_host: str = "127.0.0.1",
        client_port: int = 8765,
        bot_host: str = "127.0.0.1",
        bot_port: int = 8766,
        token: str | None = None,
    ) -> None:
        self.client_host = client_host
        self.client_port = client_port
        self.bot_host = bot_host
        self.bot_port = bot_port
        self.token = token or None
        self._clients: set[Any] = set()
        self._bots: set[Any] = set()
        self._conversation_routes: dict[str, Any] = {}
        self._request_routes: dict[str, Any] = {}

    async def start(self) -> None:
        """Start websocket listeners for clients and nanobot."""
        print(
            f"bridge listening: clients=ws://{self.client_host}:{self.client_port} "
            f"nanobot=ws://{self.bot_host}:{self.bot_port}"
        )
        async with (
            websockets.serve(self._handle_client, self.client_host, self.client_port),
            websockets.serve(self._handle_bot, self.bot_host, self.bot_port),
        ):
            await asyncio.Future()

    async def _handle_client(self, ws: Any) -> None:
        if not await self._authenticate(ws):
            return
        self._clients.add(ws)
        try:
            async for raw in ws:
                packet = decode_packet(raw)
                msg_type = packet.get("type")
                if msg_type == "message":
                    await self._handle_client_message(ws, packet)
                elif msg_type == "cancel":
                    await self._handle_client_cancel(ws, packet)
                elif msg_type == "ping":
                    await ws.send(encode_packet({"type": "pong"}))
                else:
                    await self._send_error(
                        ws,
                        code="unsupported_client_message",
                        message=f"Unsupported client packet type: {msg_type!r}",
                    )
        finally:
            self._clients.discard(ws)
            self._drop_routes_for_socket(ws)

    async def _handle_bot(self, ws: Any) -> None:
        if not await self._authenticate(ws):
            return
        self._bots.add(ws)
        try:
            async for raw in ws:
                packet = decode_packet(raw)
                await self._forward_bot_event(packet)
        finally:
            self._bots.discard(ws)

    async def _authenticate(self, ws: Any) -> bool:
        if not self.token:
            return True
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            packet = decode_packet(raw)
        except Exception:
            await ws.close(code=4001, reason="auth timeout")
            return False
        if packet.get("type") != "auth" or packet.get("token") != self.token:
            await ws.close(code=4003, reason="invalid token")
            return False
        await ws.send(encode_packet({"type": "auth_ok"}))
        return True

    async def _handle_client_message(self, ws: Any, packet: dict[str, Any]) -> None:
        bot = self._pick_bot()
        if bot is None:
            await self._send_error(ws, code="no_bot_connected", message="No nanobot bridge channel connected")
            return

        conversation_id = str(packet.get("conversation_id") or "").strip()
        user_id = str(packet.get("user_id") or "").strip()
        tenant_id = str(packet.get("tenant_id") or "default")
        content = str(packet.get("content") or "")
        if not conversation_id or not user_id:
            await self._send_error(
                ws,
                code="invalid_message",
                message="conversation_id and user_id are required",
            )
            return

        request_id = str(packet.get("request_id") or make_request_id())
        route_key = self._conversation_key(tenant_id, conversation_id)
        self._conversation_routes[route_key] = ws
        self._request_routes[request_id] = ws

        outbound = {
            "type": "inbound_message",
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "tenant_id": tenant_id,
            "conversation_id": conversation_id,
            "session_key": make_session_key(tenant_id, conversation_id),
            "channel": "bridge",
            "sender_id": user_id,
            "chat_id": conversation_id,
            "content": content,
            "attachments": packet.get("attachments") or [],
            "metadata": packet.get("metadata") or {},
        }
        await bot.send(encode_packet(outbound))
        await ws.send(
            encode_packet(
                {
                    "type": "ack",
                    "request_id": request_id,
                    "conversation_id": conversation_id,
                }
            )
        )

    async def _handle_client_cancel(self, ws: Any, packet: dict[str, Any]) -> None:
        bot = self._pick_bot()
        if bot is None:
            await self._send_error(ws, code="no_bot_connected", message="No nanobot bridge channel connected")
            return

        conversation_id = str(packet.get("conversation_id") or "").strip()
        tenant_id = str(packet.get("tenant_id") or "default")
        request_id = str(packet.get("request_id") or "")
        user_id = str(packet.get("user_id") or "").strip()
        if not conversation_id:
            await self._send_error(ws, code="invalid_cancel", message="conversation_id is required")
            return

        self._conversation_routes[self._conversation_key(tenant_id, conversation_id)] = ws
        if request_id:
            self._request_routes[request_id] = ws

        await bot.send(
            encode_packet(
                {
                    "type": "cancel",
                    "version": PROTOCOL_VERSION,
                    "request_id": request_id,
                    "tenant_id": tenant_id,
                    "conversation_id": conversation_id,
                    "session_key": make_session_key(tenant_id, conversation_id),
                    "sender_id": user_id,
                }
            )
        )

    async def _forward_bot_event(self, packet: dict[str, Any]) -> None:
        if packet.get("type") not in {"progress", "final", "error", "cancelled"}:
            return
        ws = self._resolve_client(packet)
        if ws is None:
            return
        await ws.send(encode_packet(packet))

    async def _send_error(self, ws: Any, *, code: str, message: str) -> None:
        await ws.send(encode_packet({"type": "error", "code": code, "content": message}))

    def _resolve_client(self, packet: dict[str, Any]) -> Any | None:
        request_id = str(packet.get("request_id") or "")
        if request_id and request_id in self._request_routes:
            return self._request_routes[request_id]
        tenant_id = str(packet.get("tenant_id") or "default")
        conversation_id = str(packet.get("conversation_id") or "")
        if conversation_id:
            return self._conversation_routes.get(self._conversation_key(tenant_id, conversation_id))
        return None

    def _pick_bot(self) -> Any | None:
        return next(iter(self._bots), None)

    def _drop_routes_for_socket(self, ws: Any) -> None:
        self._conversation_routes = {
            key: value for key, value in self._conversation_routes.items() if value is not ws
        }
        self._request_routes = {
            key: value for key, value in self._request_routes.items() if value is not ws
        }

    @staticmethod
    def _conversation_key(tenant_id: str, conversation_id: str) -> str:
        return f"{tenant_id}:{conversation_id}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone bridge service for nanobot bridge channel")
    parser.add_argument("--client-host", default="127.0.0.1")
    parser.add_argument("--client-port", type=int, default=8765)
    parser.add_argument("--bot-host", default="127.0.0.1")
    parser.add_argument("--bot-port", type=int, default=8766)
    parser.add_argument("--token", default="")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    service = BridgeService(
        client_host=args.client_host,
        client_port=args.client_port,
        bot_host=args.bot_host,
        bot_port=args.bot_port,
        token=args.token or None,
    )
    await service.start()


if __name__ == "__main__":
    asyncio.run(main())
