from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import websockets
from loguru import logger

from bucket_runtime.config import (
    DEFAULT_CONFIG_PATH,
    INSTANCE_HOST,
    INSTANCE_STOP_GRACE_SECONDS,
    MAX_PROCESSES_PER_BUCKET,
    OUTBOUND_GATEWAY_URL,
    OUTBOUND_TIMEOUT,
    SKILLS_ROOT,
    SOURCE_ROOT,
    TEMPLATES_ROOT,
    WORKSPACE_ROOT,
)
from bucket_runtime.port_allocator import PortAllocator
from bucket_runtime.process_utils import subprocess_group_kwargs, terminate_process_group
from bucket_runtime.workspace_manager import WorkspaceManager
from container_up.frontend_config import frontend_config_for


@dataclass
class UserProcess:
    frontend_id: str
    user_id: str
    user_key: str
    workspace_path: Path
    port: int
    process: asyncio.subprocess.Process
    started_at: float
    last_active_at: float
    websocket: Any | None = None
    relay_task: asyncio.Task[None] | None = None
    log_task: asyncio.Task[None] | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ProcessManager:
    def __init__(
        self,
        *,
        workspace_manager: WorkspaceManager | None = None,
        port_allocator: PortAllocator | None = None,
        idle_ttl: int,
    ) -> None:
        self._workspace_manager = workspace_manager or WorkspaceManager(
            WORKSPACE_ROOT,
            TEMPLATES_ROOT,
        )
        self._port_allocator = port_allocator or PortAllocator(20000, 29999)
        self._idle_ttl = idle_ttl
        self._processes: dict[str, UserProcess] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _user_key(self, frontend_id: str, user_id: str) -> str:
        return f"{frontend_id}:{user_id}"

    async def close(self) -> None:
        for user_key in list(self._processes):
            await self.stop_process(user_key)

    async def get_or_start(self, frontend_id: str, user_id: str) -> UserProcess:
        user_key = self._user_key(frontend_id, user_id)
        lock = self._locks.setdefault(user_key, asyncio.Lock())
        async with lock:
            existing = self._processes.get(user_key)
            if existing is not None and existing.process.returncode is None:
                existing.last_active_at = time.time()
                await self._ensure_instance_socket(existing)
                return existing

            if len(self._processes) >= MAX_PROCESSES_PER_BUCKET and user_key not in self._processes:
                raise RuntimeError("bucket has reached max process capacity")

            frontend_config = frontend_config_for(frontend_id)
            template_root = (
                frontend_config.template_dir
                if frontend_config is not None and frontend_config.template_dir is not None
                else TEMPLATES_ROOT
            )
            config_path = (
                frontend_config.config_path
                if frontend_config is not None and frontend_config.config_path is not None
                else DEFAULT_CONFIG_PATH
            )
            if config_path is None:
                raise RuntimeError(f"frontend {frontend_id} does not define a config_path")
            workspace_path = self._workspace_manager.ensure_workspace(
                frontend_id,
                user_id,
                template_root=template_root,
            )
            port = self._port_allocator.allocate(user_key)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "bucket_runtime.local_service",
                "--config",
                str(config_path),
                "--workspace",
                str(workspace_path),
                "--host",
                INSTANCE_HOST,
                "--port",
                str(port),
                cwd=str(SOURCE_ROOT),
                env=self._build_process_env(frontend_id, frontend_config),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **subprocess_group_kwargs(),
            )
            try:
                await self._wait_instance_ready(port)
            except Exception:
                await terminate_process_group(process, timeout=INSTANCE_STOP_GRACE_SECONDS)
                self._port_allocator.release(user_key)
                raise

            instance = UserProcess(
                frontend_id=frontend_id,
                user_id=user_id,
                user_key=user_key,
                workspace_path=workspace_path,
                port=port,
                process=process,
                started_at=time.time(),
                last_active_at=time.time(),
            )
            instance.log_task = asyncio.create_task(self._drain_logs(instance))
            self._processes[user_key] = instance
            await self._ensure_instance_socket(instance)
            logger.info("started user process user_key={} port={}", user_key, port)
            return instance

    def _build_process_env(self, frontend_id: str, frontend_config: Any | None) -> dict[str, str]:
        env = os.environ.copy()
        extra_pythonpath = str(SOURCE_ROOT)
        env["PYTHONPATH"] = (
            f"{extra_pythonpath}:{env['PYTHONPATH']}"
            if env.get("PYTHONPATH")
            else extra_pythonpath
        )
        env["HOST_WORKSPACE_ROOT"] = str(WORKSPACE_ROOT)
        env["CHILD_WORKSPACE_TARGET"] = str(WORKSPACE_ROOT)
        env["NANOBOT_WORKSPACE_LAYOUT"] = "frontend-user"
        env["TEMPLATE_DIR"] = str(
            frontend_config.template_dir
            if frontend_config is not None and frontend_config.template_dir is not None
            else TEMPLATES_ROOT
        )
        env["BUILTIN_SKILLS_DIR"] = str(
            frontend_config.builtin_skills_dir
            if frontend_config is not None and frontend_config.builtin_skills_dir is not None
            else SKILLS_ROOT
        )
        return env

    async def _wait_instance_ready(self, port: int) -> None:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                async with websockets.connect(
                    f"ws://{INSTANCE_HOST}:{port}",
                    proxy=None,
                    ping_interval=None,
                    ping_timeout=None,
                ):
                    return
            except Exception:
                await asyncio.sleep(0.2)
        raise RuntimeError(f"user instance on port {port} did not become ready")

    async def _ensure_instance_socket(self, instance: UserProcess) -> None:
        if instance.websocket is not None:
            return
        websocket = await websockets.connect(
            f"ws://{INSTANCE_HOST}:{instance.port}",
            proxy=None,
            ping_interval=None,
            ping_timeout=None,
        )
        instance.websocket = websocket
        instance.relay_task = asyncio.create_task(self._relay_instance(instance, websocket))

    async def _relay_instance(self, instance: UserProcess, websocket: Any) -> None:
        try:
            async for raw in websocket:
                packet = json.loads(raw)
                if str(packet.get("type") or "") != "outbound_message":
                    continue
                await self._forward_outbound(instance, packet)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("instance relay failed user_key={}", instance.user_key)
        finally:
            if instance.websocket is websocket:
                instance.websocket = None

    async def _forward_outbound(self, instance: UserProcess, packet: dict[str, Any]) -> None:
        metadata = dict(packet.get("metadata") or {})
        metadata.setdefault("frontend_id", instance.frontend_id)
        metadata.setdefault("usr_id", instance.user_id)
        async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT) as client:
            response = await client.post(
                OUTBOUND_GATEWAY_URL,
                json={
                    "frontend_id": instance.frontend_id,
                    "user_id": instance.user_id,
                    "chat_id": str(packet.get("chat_id") or ""),
                    "content": str(packet.get("content") or ""),
                    "attachments": list(packet.get("attachments") or []),
                    "metadata": metadata,
                    "raw": {"source": "bucket-runtime"},
                },
            )
            response.raise_for_status()

    async def forward_inbound(
        self,
        frontend_id: str,
        user_id: str,
        packet: dict[str, Any],
    ) -> dict[str, Any]:
        instance = await self.get_or_start(frontend_id, user_id)
        instance.last_active_at = time.time()
        await self._send_instance(
            instance,
            {
                "type": "inbound_message",
                "channel": str(packet.get("channel") or "bridge"),
                "chat_id": str(packet.get("chat_id") or "default"),
                "content": str(packet.get("content") or ""),
                "attachments": list(packet.get("attachments") or []),
                "metadata": dict(packet.get("metadata") or {}),
            },
        )
        return {"status": "accepted", "frontend_id": frontend_id, "user_id": user_id}

    async def forward_cancel(
        self,
        frontend_id: str,
        user_id: str,
        packet: dict[str, Any],
    ) -> dict[str, Any]:
        instance = self._processes.get(self._user_key(frontend_id, user_id))
        if instance is None or instance.process.returncode is not None:
            return {"status": "accepted", "frontend_id": frontend_id, "user_id": user_id}
        instance.last_active_at = time.time()
        await self._ensure_instance_socket(instance)
        await self._send_instance(
            instance,
            {
                "type": "cancel",
                "channel": str(packet.get("channel") or "bridge"),
                "chat_id": str(packet.get("chat_id") or "default"),
                "metadata": dict(packet.get("metadata") or {}),
            },
        )
        return {"status": "accepted", "frontend_id": frontend_id, "user_id": user_id}

    async def _send_instance(self, instance: UserProcess, packet: dict[str, Any]) -> None:
        await self._ensure_instance_socket(instance)
        async with instance.send_lock:
            if instance.websocket is None:
                raise RuntimeError(f"instance websocket unavailable for {instance.user_key}")
            await instance.websocket.send(json.dumps(packet, ensure_ascii=False))

    async def _drain_logs(self, instance: UserProcess) -> None:
        if instance.process.stdout is None:
            return
        try:
            while True:
                line = await instance.process.stdout.readline()
                if not line:
                    return
                logger.info("[{}] {}", instance.user_key, line.decode("utf-8", errors="replace").rstrip())
        except asyncio.CancelledError:
            raise

    async def reap_idle_processes(self) -> None:
        cutoff = time.time() - self._idle_ttl
        for user_key, instance in list(self._processes.items()):
            if instance.last_active_at < cutoff:
                await self.stop_process(user_key)

    async def stop_process(self, user_key: str) -> None:
        instance = self._processes.pop(user_key, None)
        if instance is None:
            return
        if instance.relay_task is not None:
            instance.relay_task.cancel()
            await asyncio.gather(instance.relay_task, return_exceptions=True)
        if instance.log_task is not None:
            instance.log_task.cancel()
            await asyncio.gather(instance.log_task, return_exceptions=True)
        if instance.websocket is not None:
            try:
                await instance.websocket.close()
            except Exception:
                pass
        await terminate_process_group(instance.process, timeout=INSTANCE_STOP_GRACE_SECONDS)
        self._port_allocator.release(user_key)

    def status(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for instance in self._processes.values():
            if instance.process.returncode is not None:
                continue
            items.append(
                {
                    "frontend_id": instance.frontend_id,
                    "user_id": instance.user_id,
                    "port": instance.port,
                    "last_active_at": instance.last_active_at,
                    "workspace_path": str(instance.workspace_path),
                }
            )
        items.sort(key=lambda item: (item["frontend_id"], item["user_id"]))
        return items
