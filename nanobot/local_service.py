"""Local websocket relay that fronts a standard nanobot gateway process."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

import websockets
from loguru import logger


class LocalNanobotService:
    """Expose a local bridge websocket while delegating runtime to `nanobot gateway`."""

    def __init__(self, *, config_path: Path, workspace_path: Path, host: str, port: int) -> None:
        self.config_path = config_path
        self.workspace_path = workspace_path
        self.host = host
        self.port = port

        self._server: Any = None
        self._router_ws: Any = None
        self._gateway_ws: Any = None
        self._router_send_lock = asyncio.Lock()
        self._gateway_send_lock = asyncio.Lock()
        self._gateway_ready = asyncio.Event()
        self._gateway_process: asyncio.subprocess.Process | None = None
        self._gateway_watch_task: asyncio.Task[None] | None = None
        self._stopping = False

        self._bridge_token = secrets.token_hex(16)
        self._gateway_config_path = (
            self.workspace_path / ".nanobot-local" / "gateway.bridge.config.json"
        )

    async def start(self) -> None:
        self._prepare_gateway_config()
        self._server = await websockets.serve(
            self._handle_client,
            self.host,
            self.port,
            ping_interval=None,
            ping_timeout=None,
        )
        await self._spawn_gateway()
        logger.info("Local gateway relay listening on ws://{}:{}", self.host, self.port)
        await self._server.wait_closed()

    async def close(self) -> None:
        self._stopping = True
        if self._gateway_watch_task is not None and not self._gateway_watch_task.done():
            self._gateway_watch_task.cancel()
            await asyncio.gather(self._gateway_watch_task, return_exceptions=True)

        for websocket in (self._router_ws, self._gateway_ws):
            if websocket is not None:
                try:
                    await websocket.close()
                except Exception:
                    pass

        self._router_ws = None
        self._gateway_ws = None
        self._gateway_ready.clear()

        if self._gateway_process is not None and self._gateway_process.returncode is None:
            self._gateway_process.terminate()
            try:
                await asyncio.wait_for(self._gateway_process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._gateway_process.kill()
                await self._gateway_process.wait()
        self._gateway_process = None

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def _prepare_gateway_config(self) -> None:
        data = json.loads(self.config_path.read_text(encoding="utf-8"))

        channels = dict(data.get("channels") or {})
        for name, section in list(channels.items()):
            if name in {"sendProgress", "sendToolHints", "sendMaxRetries", "bridge"}:
                continue
            if isinstance(section, dict):
                channels[name] = {**section, "enabled": False}

        bridge = dict(channels.get("bridge") or {})
        bridge["enabled"] = True
        bridge["bridgeUrl"] = f"ws://{self.host}:{self.port}"
        bridge["bridgeToken"] = self._bridge_token
        bridge["allowFrom"] = ["*"]
        channels["bridge"] = bridge
        data["channels"] = channels

        agents = dict(data.get("agents") or {})
        defaults = dict(agents.get("defaults") or {})
        defaults["workspace"] = str(self.workspace_path)
        agents["defaults"] = defaults
        data["agents"] = agents

        self._gateway_config_path.parent.mkdir(parents=True, exist_ok=True)
        self._gateway_config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def _spawn_gateway(self) -> None:
        env = os.environ.copy()
        env["BRIDGE_SESSION_ID"] = ""
        env["BRIDGE_CONTAINER_NAME"] = ""
        env["BRIDGE_ORG_ID"] = ""
        env["PARENT_BRIDGE_URL"] = ""

        self._gateway_process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "nanobot",
            "gateway",
            "--config",
            str(self._gateway_config_path),
            "--workspace",
            str(self.workspace_path),
            "--port",
            str(self.port),
            env=env,
        )
        self._gateway_watch_task = asyncio.create_task(self._watch_gateway_process())

    async def _watch_gateway_process(self) -> None:
        assert self._gateway_process is not None
        returncode = await self._gateway_process.wait()
        if self._stopping:
            return
        logger.error("nanobot gateway exited unexpectedly with code {}", returncode)
        if self._server is not None:
            self._server.close()

    async def _handle_client(self, websocket: Any) -> None:
        first_packet: dict[str, Any] | None = None
        try:
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                raw = None

            if raw is not None:
                try:
                    first_packet = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send(
                        json.dumps({"type": "error", "content": "invalid json"})
                    )
                    return

            if first_packet is not None and self._is_gateway_handshake(first_packet):
                await self._run_gateway_session(websocket, first_packet)
            else:
                await self._run_router_session(websocket, first_packet)
        except websockets.ConnectionClosed:
            return

    @staticmethod
    def _is_gateway_handshake(packet: dict[str, Any]) -> bool:
        return str(packet.get("type") or "") in {"auth", "register"}

    async def _run_gateway_session(self, websocket: Any, packet: dict[str, Any]) -> None:
        packet_type = str(packet.get("type") or "")
        token = str(packet.get("token") or "")
        if token != self._bridge_token:
            await websocket.send(json.dumps({"type": "error", "content": "invalid bridge token"}))
            await websocket.close(code=4003, reason="invalid token")
            return

        if packet_type == "register":
            await websocket.send(json.dumps({"type": "register_ok", "version": 2}))
        else:
            await websocket.send(json.dumps({"type": "auth_ok"}))

        self._gateway_ws = websocket
        self._gateway_ready.set()
        try:
            async for raw in websocket:
                message = json.loads(raw)
                if str(message.get("type") or "") == "outbound_message":
                    await self._send_router(message)
        finally:
            if self._gateway_ws is websocket:
                self._gateway_ws = None
                self._gateway_ready.clear()

    async def _run_router_session(self, websocket: Any, first_packet: dict[str, Any] | None) -> None:
        self._router_ws = websocket
        try:
            if first_packet is not None:
                await self._forward_to_gateway(first_packet)
            async for raw in websocket:
                packet = json.loads(raw)
                await self._forward_to_gateway(packet)
        finally:
            if self._router_ws is websocket:
                self._router_ws = None

    async def _forward_to_gateway(self, packet: dict[str, Any]) -> None:
        packet_type = str(packet.get("type") or "")
        if packet_type not in {"inbound_message", "cancel"}:
            raise RuntimeError(f"unsupported router packet type: {packet_type}")

        await asyncio.wait_for(self._gateway_ready.wait(), timeout=30)
        async with self._gateway_send_lock:
            if self._gateway_ws is None:
                raise RuntimeError("gateway bridge websocket is unavailable")
            await self._gateway_ws.send(json.dumps(packet, ensure_ascii=False))

    async def _send_router(self, packet: dict[str, Any]) -> None:
        async with self._router_send_lock:
            if self._router_ws is None:
                logger.warning("Dropping outbound packet because org router is disconnected")
                return
            await self._router_ws.send(json.dumps(packet, ensure_ascii=False))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local websocket relay for nanobot gateway.")
    parser.add_argument("--config", required=True, help="Path to shared config.json")
    parser.add_argument("--workspace", required=True, help="Workspace directory")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, required=True, help="Bind port")
    return parser.parse_args()


async def _main_async() -> None:
    args = parse_args()
    service = LocalNanobotService(
        config_path=Path(args.config).expanduser().resolve(),
        workspace_path=Path(args.workspace).expanduser().resolve(),
        host=args.host,
        port=args.port,
    )
    try:
        await service.start()
    finally:
        await service.close()


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
