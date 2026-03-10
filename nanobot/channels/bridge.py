"""Generic remote bridge channel implementation."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import BridgeConfig


class BridgeChannel(BaseChannel):
    """Channel that connects nanobot to an external bridge over WebSocket."""

    name = "bridge"

    def __init__(self, config: BridgeConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: BridgeConfig = config
        self._ws = None
        self._connected = False

    async def start(self) -> None:
        """Connect to the bridge and consume inbound packets forever."""
        import websockets

        self._running = True
        logger.info("Connecting to bridge at {}...", self.config.bridge_url)

        while self._running:
            try:
                async with websockets.connect(self.config.bridge_url, proxy=None) as ws:
                    self._ws = ws
                    if self.config.bridge_token:
                        await ws.send(json.dumps({"type": "auth", "token": self.config.bridge_token}))
                    self._connected = True
                    logger.info("Connected to bridge")

                    async for raw in ws:
                        try:
                            await self._handle_bridge_message(raw)
                        except Exception:
                            logger.exception("Error handling bridge packet")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._ws = None
                logger.warning("Bridge connection error: {}", e)
                if self._running:
                    logger.info("Reconnecting to bridge in 5 seconds...")
                    await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop the bridge channel."""
        self._running = False
        self._connected = False
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send nanobot output back to the bridge."""
        if not self._ws or not self._connected:
            logger.warning("Bridge not connected")
            return

        packet = self._encode_outbound(msg)
        try:
            await self._ws.send(json.dumps(packet, ensure_ascii=False))
        except Exception as e:
            logger.error("Error sending bridge packet: {}", e)

    async def _handle_bridge_message(self, raw: str) -> None:
        """Handle a packet from the bridge."""
        try:
            packet = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from bridge: {}", raw[:100])
            return

        msg_type = packet.get("type")
        if msg_type == "inbound_message":
            await self._publish_bridge_inbound(packet)
        elif msg_type == "cancel":
            await self._publish_bridge_cancel(packet)
        elif msg_type in {"auth_ok", "pong"}:
            return
        elif msg_type == "error":
            logger.error("Bridge error: {}", packet.get("content") or packet.get("error"))

    async def _publish_bridge_inbound(self, packet: dict[str, Any]) -> None:
        metadata = dict(packet.get("metadata") or {})
        metadata.update(
            {
                "request_id": packet.get("request_id", ""),
                "tenant_id": packet.get("tenant_id", "default"),
                "conversation_id": packet.get("conversation_id", ""),
            }
        )
        await self._handle_message(
            sender_id=str(packet.get("sender_id") or "user"),
            chat_id=str(packet.get("chat_id") or packet.get("conversation_id") or "remote"),
            content=str(packet.get("content") or ""),
            media=[str(item) for item in packet.get("attachments") or []],
            metadata=metadata,
            session_key=str(packet.get("session_key") or "") or None,
        )

    async def _publish_bridge_cancel(self, packet: dict[str, Any]) -> None:
        metadata = {
            "request_id": packet.get("request_id", ""),
            "tenant_id": packet.get("tenant_id", "default"),
            "conversation_id": packet.get("conversation_id", ""),
        }
        await self._handle_message(
            sender_id=str(packet.get("sender_id") or "remote-control"),
            chat_id=str(packet.get("conversation_id") or "remote"),
            content="/stop",
            metadata=metadata,
            session_key=str(packet.get("session_key") or "") or None,
        )

    def _encode_outbound(self, msg: OutboundMessage) -> dict[str, Any]:
        metadata = dict(msg.metadata or {})
        packet: dict[str, Any] = {
            "type": self._event_type(msg),
            "request_id": str(metadata.get("request_id") or ""),
            "tenant_id": str(metadata.get("tenant_id") or "default"),
            "conversation_id": str(metadata.get("conversation_id") or msg.chat_id),
            "content": msg.content,
        }
        if metadata.get("_progress"):
            packet["kind"] = "tool_hint" if metadata.get("_tool_hint") else "reasoning"
        if msg.media:
            packet["attachments"] = list(msg.media)
        return packet

    @staticmethod
    def _event_type(msg: OutboundMessage) -> str:
        metadata = msg.metadata or {}
        if metadata.get("_progress"):
            return "progress"
        if msg.content.startswith("⏹ Stopped") or msg.content == "No active task to stop.":
            return "cancelled"
        if msg.content == "Sorry, I encountered an error.":
            return "error"
        return "final"
