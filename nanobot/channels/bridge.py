"""Generic remote bridge channel implementation."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base


class BridgeConfig(Base):
    """Generic remote bridge channel configuration."""

    enabled: bool = False
    bridge_url: str = "ws://localhost:8765"
    bridge_token: str = ""
    allow_from: list[str] = Field(default_factory=list)


class BridgeChannel(BaseChannel):
    """Channel that connects nanobot to an external bridge over WebSocket."""

    name = "bridge"
    display_name = "Bridge"
    _PROTOCOL_VERSION = 2
    _DELIVERY_TARGET_SEPARATOR = ":::"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return BridgeConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = BridgeConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: BridgeConfig = config
        self._ws = None
        self._connected = False
        self._session_id = os.getenv("BRIDGE_SESSION_ID", "").strip()
        self._container_name = os.getenv("BRIDGE_CONTAINER_NAME", "").strip()

    async def start(self) -> None:
        """Connect to the bridge and consume inbound packets forever."""
        import websockets

        self._running = True
        logger.info("Connecting to bridge at {}...", self.config.bridge_url)

        while self._running:
            try:
                async with websockets.connect(self.config.bridge_url, proxy=None) as ws:
                    self._ws = ws
                    if packet := self._build_handshake_packet():
                        await ws.send(json.dumps(packet, ensure_ascii=False))
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
            logger.warning("Bridge not connected, falling back to proactive send")
            await self.send_proactive_message(
                self.config,
                to=msg.chat_id,
                content=msg.content,
                metadata=msg.metadata,
            )
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
        elif msg_type in {"auth_ok", "register_ok", "pong"}:
            return
        elif msg_type == "register_reject":
            logger.error("Bridge registration rejected: {}", packet.get("error") or packet.get("content"))
        elif msg_type == "error":
            logger.error("Bridge error: {}", packet.get("content") or packet.get("error"))

    async def _publish_bridge_inbound(self, packet: dict[str, Any]) -> None:
        metadata = dict(packet.get("metadata") or {})
        sender_id = str(packet.get("sender_id") or "user")
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(packet.get("chat_id") or "remote"),
            content=str(packet.get("content") or ""),
            media=[str(item) for item in packet.get("attachments") or []],
            metadata=metadata,
            session_key=str(packet.get("session_key") or "") or None,
        )

    async def _publish_bridge_cancel(self, packet: dict[str, Any]) -> None:
        metadata = dict(packet.get("metadata") or {})
        await self._handle_message(
            sender_id=str(packet.get("sender_id") or "remote-control"),
            chat_id=str(packet.get("chat_id") or "remote"),
            content="/stop",
            metadata=metadata,
            session_key=str(packet.get("session_key") or "") or None,
        )

    def _encode_outbound(self, msg: OutboundMessage) -> dict[str, Any]:
        packet: dict[str, Any] = {
            "type": "outbound_message",
            "version": self._PROTOCOL_VERSION,
            "channel": msg.channel,
            "chat_id": msg.chat_id,
            "content": msg.content,
            "metadata": dict(msg.metadata or {}),
        }
        if msg.media:
            packet["attachments"] = list(msg.media)
        if msg.reply_to:
            packet["reply_to"] = msg.reply_to
        return packet

    def _build_handshake_packet(self) -> dict[str, Any] | None:
        if self._session_id and self._container_name:
            packet: dict[str, Any] = {
                "type": "register",
                "version": self._PROTOCOL_VERSION,
                "session_id": self._session_id,
                "container_name": self._container_name,
            }
            if self.config.bridge_token:
                packet["token"] = self.config.bridge_token
            return packet

        if self.config.bridge_token:
            return {"type": "auth", "token": self.config.bridge_token}

        return None

    @classmethod
    async def send_proactive_message(
        cls,
        config: Any,
        *,
        to: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if isinstance(config, dict):
            config = BridgeConfig.model_validate(config)
        elif not isinstance(config, BridgeConfig):
            config = BridgeConfig.model_validate(config.model_dump(by_alias=True))

        url = cls._resolve_outbound_url(config)
        if not url:
            raise RuntimeError("Bridge outbound URL is not configured")
        org_id = os.getenv("BRIDGE_ORG_ID", os.getenv("BRIDGE_SESSION_ID", "")).strip()
        if not org_id:
            raise RuntimeError("BRIDGE_ORG_ID or BRIDGE_SESSION_ID is required for proactive bridge sends")

        payload = {
            "org_id": org_id,
            "to": to,
            "content": content,
            "metadata": dict(metadata or {}),
        }
        token = config.bridge_token
        await asyncio.to_thread(cls._post_outbound_sync, url, token, payload)

    @staticmethod
    def _post_outbound_sync(url: str, token: str, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Bridge-Token"] = token
        request = Request(url, data=data, headers=headers, method="POST")
        with urlopen(request, timeout=15) as response:
            if response.status >= 400:
                body = response.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"bridge outbound rejected with {response.status}: {body}")

    @classmethod
    def _resolve_outbound_url(cls, config: BridgeConfig) -> str:
        parent_bridge_url = os.getenv("PARENT_BRIDGE_URL", "").strip()
        if not parent_bridge_url:
            raise RuntimeError("PARENT_BRIDGE_URL is required for proactive bridge sends")
        return cls._bridge_ws_to_outbound_http(parent_bridge_url)

    @staticmethod
    def _bridge_ws_to_outbound_http(bridge_url: str) -> str:
        parsed = urlparse(bridge_url)
        if not parsed.scheme or not parsed.netloc:
            return ""
        http_scheme = "https" if parsed.scheme == "wss" else "http"
        return urlunparse((http_scheme, parsed.netloc, "/api/bridge/outbound", "", "", ""))

    @classmethod
    def _compose_delivery_target(cls, sender_id: str, conversation_id: str) -> str:
        return f"{sender_id}{cls._DELIVERY_TARGET_SEPARATOR}{conversation_id}"

    @classmethod
    def split_delivery_target(cls, value: str) -> tuple[str, str]:
        sender_id, separator, conversation_id = value.partition(cls._DELIVERY_TARGET_SEPARATOR)
        if not separator or not sender_id or not conversation_id:
            raise ValueError("invalid bridge delivery target")
        return sender_id, conversation_id
