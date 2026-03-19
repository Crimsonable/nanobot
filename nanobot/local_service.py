"""Local websocket service for one isolated nanobot instance."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import websockets
from loguru import logger

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.cli.commands import _make_provider
from nanobot.config.loader import load_config, set_config_path
from nanobot.config.paths import get_cron_dir
from nanobot.cron.service import CronService
from nanobot.session.manager import SessionManager
from nanobot.utils.helpers import sync_workspace_templates


class LocalNanobotService:
    """Expose AgentLoop.process_direct() over a local websocket protocol."""

    def __init__(self, *, config_path: Path, workspace_path: Path, host: str, port: int) -> None:
        self.config_path = config_path
        self.workspace_path = workspace_path
        self.host = host
        self.port = port
        self._server: Any = None
        self._request_tasks: dict[str, asyncio.Task[str]] = {}
        self._request_sessions: dict[str, str] = {}
        self._request_sockets: dict[str, Any] = {}
        self._processing_lock = asyncio.Lock()

        set_config_path(self.config_path)
        self.config = load_config(self.config_path)
        self.config.agents.defaults.workspace = str(self.workspace_path)
        sync_workspace_templates(self.workspace_path)

        self.bus = MessageBus()
        self.provider = _make_provider(self.config)
        self.cron = CronService(get_cron_dir() / "jobs.json")
        self.agent = AgentLoop(
            bus=self.bus,
            provider=self.provider,
            workspace=self.workspace_path,
            model=self.config.agents.defaults.model,
            max_iterations=self.config.agents.defaults.max_tool_iterations,
            context_window_tokens=self.config.agents.defaults.context_window_tokens,
            web_proxy=self.config.tools.web.proxy or None,
            exec_config=self.config.tools.exec,
            cron_service=self.cron,
            restrict_to_workspace=self.config.tools.restrict_to_workspace,
            session_manager=SessionManager(self.workspace_path),
            mcp_servers=self.config.tools.mcp_servers,
            channels_config=self.config.channels,
        )

    async def start(self) -> None:
        await self.agent._connect_mcp()
        self._server = await websockets.serve(self._handle_client, self.host, self.port, ping_interval=20)
        logger.info("Local nanobot service listening on ws://{}:{}", self.host, self.port)
        await self._server.wait_closed()

    async def close(self) -> None:
        for task in list(self._request_tasks.values()):
            if not task.done():
                task.cancel()
        await self.agent.close_mcp()
        self.cron.stop()
        self.agent.stop()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_client(self, websocket: Any) -> None:
        try:
            async for raw in websocket:
                packet = json.loads(raw)
                msg_type = str(packet.get("type") or "")
                if msg_type == "message":
                    await self._handle_message(websocket, packet)
                elif msg_type == "cancel":
                    await self._handle_cancel(websocket, packet)
                else:
                    await websocket.send(json.dumps({"type": "error", "content": "unsupported packet type"}))
        except websockets.ConnectionClosed:
            return

    async def _handle_message(self, websocket: Any, packet: dict[str, Any]) -> None:
        request_id = str(packet.get("request_id") or "")
        session_key = str(packet.get("session_key") or packet.get("chat_id") or "remote:default")
        chat_id = str(packet.get("chat_id") or "remote")
        channel = str(packet.get("channel") or "bridge")
        metadata = dict(packet.get("metadata") or {})
        content = str(packet.get("content") or "")
        media = [str(item) for item in packet.get("attachments") or []]

        if not request_id:
            await websocket.send(json.dumps({"type": "error", "content": "missing request_id"}))
            return

        async def on_progress(progress: str, *, tool_hint: bool = False) -> None:
            event = {
                "type": "progress",
                "request_id": request_id,
                "content": progress,
                "kind": "tool_hint" if tool_hint else "reasoning",
            }
            await websocket.send(json.dumps(event, ensure_ascii=False))

        async def run_request() -> str:
            async with self._processing_lock:
                return await self.agent.process_direct(
                    content,
                    session_key=session_key,
                    channel=channel,
                    chat_id=chat_id,
                    on_progress=on_progress,
                )

        task = asyncio.create_task(run_request())
        self._request_tasks[request_id] = task
        self._request_sessions[request_id] = session_key
        self._request_sockets[request_id] = websocket
        try:
            result = await task
            await websocket.send(
                json.dumps(
                    {
                        "type": "final",
                        "request_id": request_id,
                        "content": result,
                        "metadata": metadata,
                    },
                    ensure_ascii=False,
                )
            )
        except asyncio.CancelledError:
            await websocket.send(
                json.dumps(
                    {
                        "type": "cancelled",
                        "request_id": request_id,
                        "content": "Request cancelled.",
                        "metadata": metadata,
                    },
                    ensure_ascii=False,
                )
            )
        except Exception:
            logger.exception("Local request failed: {}", request_id)
            await websocket.send(
                json.dumps(
                    {
                        "type": "error",
                        "request_id": request_id,
                        "content": "Sorry, I encountered an error.",
                        "metadata": metadata,
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            self._request_tasks.pop(request_id, None)
            self._request_sessions.pop(request_id, None)
            self._request_sockets.pop(request_id, None)

    async def _handle_cancel(self, websocket: Any, packet: dict[str, Any]) -> None:
        request_id = str(packet.get("request_id") or "")
        session_key = str(packet.get("session_key") or "")
        cancelled = 0

        if request_id and (task := self._request_tasks.get(request_id)) is not None:
            if not task.done():
                task.cancel()
                cancelled += 1

        if session_key:
            for active_request_id, active_session_key in list(self._request_sessions.items()):
                if active_session_key != session_key:
                    continue
                task = self._request_tasks.get(active_request_id)
                if task is not None and not task.done():
                    task.cancel()
                    cancelled += 1
            cancelled += await self.agent.subagents.cancel_by_session(session_key)

        await websocket.send(
            json.dumps(
                {
                    "type": "cancel_ack",
                    "request_id": request_id,
                    "cancelled": cancelled,
                },
                ensure_ascii=False,
            )
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local websocket nanobot instance service.")
    parser.add_argument("--config", required=True, help="Path to config.json")
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
