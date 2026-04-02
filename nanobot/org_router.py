"""Organization-level bridge router that manages per-user nanobot instances."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets
from loguru import logger


PARENT_BRIDGE_URL = os.getenv("PARENT_BRIDGE_URL", "ws://container-up:8080/ws/bridge")
ORG_ROOT = Path(os.getenv("ORG_ROOT", "/app/nanobot_workspaces")).expanduser().resolve()
ORG_TEMPLATE_CONFIG = Path(os.getenv("ORG_TEMPLATE_CONFIG", str(ORG_ROOT / "config.json"))).expanduser().resolve()
BRIDGE_ORG_ID = os.getenv("BRIDGE_ORG_ID", os.getenv("BRIDGE_SESSION_ID", "")).strip()
BRIDGE_CONTAINER_NAME = os.getenv("BRIDGE_CONTAINER_NAME", "").strip()
BRIDGE_TOKEN_OVERRIDE = os.getenv("BRIDGE_TOKEN_OVERRIDE", "").strip()
INSTANCE_IDLE_TIMEOUT = int(os.getenv("INSTANCE_IDLE_TIMEOUT_SECONDS", "1800"))
INSTANCE_HOST = os.getenv("INSTANCE_HOST", "127.0.0.1")
ATTACHMENTS_CACHE_DIR = Path("cache") / "attachments"


@dataclass
class UserInstance:
    user_id: str
    port: int
    process: asyncio.subprocess.Process
    config_path: Path
    workspace_path: Path
    last_active: float
    config_mtime: float
    websocket: Any | None = None
    relay_task: asyncio.Task[None] | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class OrgRouter:
    """Maintain one parent bridge connection and fan requests into user instances."""

    def __init__(self) -> None:
        self._instances: dict[str, UserInstance] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._cleanup_task: asyncio.Task[None] | None = None
        self._parent_send_lock = asyncio.Lock()
        self._parent_ws: Any = None

    async def run(self) -> None:
        if not BRIDGE_ORG_ID or not BRIDGE_CONTAINER_NAME:
            raise RuntimeError("BRIDGE_ORG_ID and BRIDGE_CONTAINER_NAME are required")
        if not ORG_TEMPLATE_CONFIG.exists():
            raise RuntimeError(f"org template config not found: {ORG_TEMPLATE_CONFIG}")

        ORG_ROOT.mkdir(parents=True, exist_ok=True)
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        while True:
            try:
                await self._run_bridge_client()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Org router bridge connection failed: {}", exc)
                await asyncio.sleep(5)

    async def close(self) -> None:
        self._parent_ws = None
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        for user_id in list(self._instances):
            await self._stop_instance(user_id)

    async def _run_bridge_client(self) -> None:
        logger.info("Connecting org router to {}", PARENT_BRIDGE_URL)
        async with websockets.connect(PARENT_BRIDGE_URL, proxy=None) as websocket:
            self._parent_ws = websocket
            await websocket.send(json.dumps(self._register_packet(), ensure_ascii=False))
            try:
                async for raw in websocket:
                    packet = json.loads(raw)
                    packet_type = str(packet.get("type") or "")
                    if packet_type in {"register_ok", "auth_ok", "pong"}:
                        continue
                    if packet_type == "inbound_message":
                        asyncio.create_task(self._handle_parent_message(packet))
                    elif packet_type == "cancel":
                        asyncio.create_task(self._handle_parent_cancel(packet))
            finally:
                if self._parent_ws is websocket:
                    self._parent_ws = None

    def _register_packet(self) -> dict[str, Any]:
        packet: dict[str, Any] = {
            "type": "register",
            "version": 2,
            "org_id": BRIDGE_ORG_ID,
            "container_name": BRIDGE_CONTAINER_NAME,
        }
        if BRIDGE_TOKEN_OVERRIDE:
            packet["token"] = BRIDGE_TOKEN_OVERRIDE
        return packet

    async def _handle_parent_message(self, packet: dict[str, Any]) -> None:
        user_id = str(packet.get("sender_id") or "user")
        instance = await self._ensure_instance(user_id)
        instance.last_active = asyncio.get_running_loop().time()
        attachments = await self._materialize_attachments(
            instance.workspace_path,
            attachment_group=str(packet.get("chat_id") or user_id),
            attachments=packet.get("attachments") or [],
        )
        await self._ensure_instance_socket(instance)
        outbound_packet = {
            "type": "inbound_message",
            "channel": str(packet.get("channel") or "bridge"),
            "sender_id": user_id,
            "chat_id": str(packet.get("chat_id") or ""),
            "session_key": str(packet.get("session_key") or "") or None,
            "content": str(packet.get("content") or ""),
            "attachments": attachments,
            "metadata": dict(packet.get("metadata") or {}),
        }
        await self._send_instance(instance, outbound_packet)

    async def _handle_parent_cancel(self, packet: dict[str, Any]) -> None:
        user_id = str(packet.get("sender_id") or "user")
        instance = self._instances.get(user_id)
        if instance is None or instance.process.returncode is not None:
            return
        await self._ensure_instance_socket(instance)
        await self._send_instance(
            instance,
            {
                "type": "cancel",
                "channel": str(packet.get("channel") or "bridge"),
                "sender_id": user_id,
                "chat_id": str(packet.get("chat_id") or ""),
                "session_key": str(packet.get("session_key") or "") or None,
                "metadata": dict(packet.get("metadata") or {}),
            },
        )

    async def _ensure_instance(self, user_id: str) -> UserInstance:
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            current = self._instances.get(user_id)
            current_mtime = self._config_mtime()
            if current is not None and current.process.returncode is None:
                if current.config_mtime == current_mtime:
                    await self._ensure_instance_socket(current)
                    return current
                await self._stop_instance(user_id)
            if current is not None:
                await self._stop_instance(user_id)

            config_path, workspace_path = self._ensure_instance_files(user_id)
            port = self._pick_free_port()
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "nanobot.local_service",
                "--config",
                str(config_path),
                "--workspace",
                str(workspace_path),
                "--host",
                INSTANCE_HOST,
                "--port",
                str(port),
                cwd=str(ORG_ROOT),
            )
            await self._wait_instance_ready(port)
            instance = UserInstance(
                user_id=user_id,
                port=port,
                process=process,
                config_path=config_path,
                workspace_path=workspace_path,
                last_active=asyncio.get_running_loop().time(),
                config_mtime=current_mtime,
            )
            self._instances[user_id] = instance
            await self._ensure_instance_socket(instance)
            logger.info("Started user instance {} on port {}", user_id, port)
            return instance

    def _ensure_instance_files(self, user_id: str) -> tuple[Path, Path]:
        user_key = self._safe_name(user_id)
        workspace_path = ORG_ROOT / user_key
        workspace_path.mkdir(parents=True, exist_ok=True)
        return ORG_TEMPLATE_CONFIG, workspace_path

    async def _wait_instance_ready(self, port: int) -> None:
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
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

    async def _ensure_instance_socket(self, instance: UserInstance) -> None:
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

    async def _relay_instance(self, instance: UserInstance, websocket: Any) -> None:
        try:
            async for raw in websocket:
                packet = json.loads(raw)
                if self._parent_ws is None:
                    logger.warning("Dropping bridge packet because parent websocket is disconnected")
                    continue
                await self._send_parent(packet)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("User instance relay failed for {}", instance.user_id)
        finally:
            if instance.websocket is websocket:
                instance.websocket = None
            if instance.relay_task is asyncio.current_task():
                instance.relay_task = None
            try:
                await websocket.close()
            except Exception:
                pass

    async def _send_instance(self, instance: UserInstance, packet: dict[str, Any]) -> None:
        async with instance.send_lock:
            if instance.websocket is None:
                await self._ensure_instance_socket(instance)
            if instance.websocket is None:
                raise RuntimeError(f"user instance websocket unavailable for {instance.user_id}")
            try:
                await instance.websocket.send(json.dumps(packet, ensure_ascii=False))
            except Exception:
                logger.exception("Failed to forward packet to user instance {}", instance.user_id)
                try:
                    await instance.websocket.close()
                except Exception:
                    pass
                instance.websocket = None
                raise

    async def _cleanup_loop(self) -> None:
        if INSTANCE_IDLE_TIMEOUT <= 0:
            return
        while True:
            await asyncio.sleep(60)
            now = asyncio.get_running_loop().time()
            for user_id, instance in list(self._instances.items()):
                if instance.process.returncode is not None:
                    await self._stop_instance(user_id)
                    continue
                if now - instance.last_active >= INSTANCE_IDLE_TIMEOUT:
                    await self._stop_instance(user_id)

    async def _stop_instance(self, user_id: str) -> None:
        instance = self._instances.pop(user_id, None)
        if instance is None:
            return
        if instance.relay_task is not None:
            instance.relay_task.cancel()
            await asyncio.gather(instance.relay_task, return_exceptions=True)
        if instance.websocket is not None:
            try:
                await instance.websocket.close()
            except Exception:
                pass
            instance.websocket = None
        if instance.process.returncode is None:
            instance.process.terminate()
            try:
                await asyncio.wait_for(instance.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                instance.process.kill()
                await instance.process.wait()
        logger.info("Stopped user instance {}", user_id)

    async def _send_parent(self, packet: dict[str, Any]) -> None:
        if self._parent_ws is None:
            return
        async with self._parent_send_lock:
            await self._parent_ws.send(json.dumps(packet, ensure_ascii=False))

    @staticmethod
    def _config_mtime() -> float:
        try:
            return ORG_TEMPLATE_CONFIG.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    @staticmethod
    def _pick_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((INSTANCE_HOST, 0))
            sock.listen(1)
            return int(sock.getsockname()[1])

    @staticmethod
    def _safe_name(value: str) -> str:
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
        cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value).strip("-.")
        return f"{cleaned[:48] or 'user'}-{digest}"

    async def _materialize_attachments(
        self,
        workspace_path: Path,
        *,
        attachment_group: str,
        attachments: list[Any],
    ) -> list[str]:
        if not attachments:
            return []

        target_dir = workspace_path / ATTACHMENTS_CACHE_DIR / self._safe_name(attachment_group)
        target_dir.mkdir(parents=True, exist_ok=True)
        local_paths: list[str] = []

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            for index, attachment in enumerate(attachments):
                if isinstance(attachment, str):
                    if attachment.startswith(("http://", "https://")):
                        saved = await self._download_attachment(
                            client,
                            attachment,
                            target_dir,
                            index=index,
                        )
                        if saved is not None:
                            local_paths.append(str(saved))
                    else:
                        local_paths.append(attachment)
                    continue

                if not isinstance(attachment, dict):
                    continue
                url = str(attachment.get("url") or "").strip()
                if not url:
                    continue
                saved = await self._download_attachment(
                    client,
                    url,
                    target_dir,
                    index=index,
                    filename=str(attachment.get("filename") or "").strip() or None,
                )
                if saved is not None:
                    local_paths.append(str(saved))

        return local_paths

    async def _download_attachment(
        self,
        client: httpx.AsyncClient,
        url: str,
        target_dir: Path,
        *,
        index: int,
        filename: str | None = None,
    ) -> Path | None:
        target = target_dir / self._attachment_filename(url, index=index, filename=filename)
        try:
            response = await client.get(url)
            response.raise_for_status()
        except Exception as exc:
            logger.warning("Attachment download failed for {}: {}", url, exc)
            return None
        target.write_bytes(response.content)
        return target

    @staticmethod
    def _attachment_filename(url: str, *, index: int, filename: str | None = None) -> str:
        candidate = filename or Path(urlparse(url).path).name or f"attachment-{index}"
        cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in candidate).strip("._")
        return cleaned or f"attachment-{index}"


async def _main_async() -> None:
    router = OrgRouter()
    try:
        await router.run()
    finally:
        await router.close()


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
