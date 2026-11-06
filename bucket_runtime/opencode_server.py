"""Single facade for the OpenCode process, HTTP API and native event stream."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
from collections.abc import Awaitable, Callable
from copy import deepcopy
from pathlib import Path

import httpx

from agent_backend.common import scoped_public_path
from agent_backend.config import RuntimeConfig

log = logging.getLogger(__name__)


class OpenCodeServer:
    """Own OpenCode lifecycle and transport without depending on the business adapter."""

    def __init__(self, config: RuntimeConfig, password: str | None = None):
        self.config = config
        self.password = password or secrets.token_urlsafe(32)
        self.rpc_token = secrets.token_urlsafe(32)
        self.process: asyncio.subprocess.Process | None = None
        self.active_config: dict | None = None
        self.connected = asyncio.Event()
        self.plugin_ready = asyncio.Event()
        self.available = asyncio.Event()
        self.subscription: asyncio.Task | None = None
        self._on_event: Callable[[dict], Awaitable[None]] | None = None
        self._lifecycle_lock = asyncio.Lock()
        oc = config.opencode
        self.http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{oc.port}",
            auth=("opencode", self.password), timeout=30, trust_env=False,
            params={"directory": str(oc.context_root)})

    @property
    def ready(self) -> bool:
        return self.available.is_set()

    def _params(self, directory: Path | None = None) -> dict[str, str]:
        return {"directory": str((directory or self.config.opencode.context_root).resolve())}

    async def start(self, on_event: Callable[[dict], Awaitable[None]]) -> None:
        async with self._lifecycle_lock:
            if self.process and self.process.returncode is None:
                raise RuntimeError("OpenCode is already running")
            self._on_event = on_event
            try:
                await self._start(self.server_config())
            except BaseException:
                await self._stop()
                raise

    async def _start(self, config: dict) -> None:
        await self._start_process(config)
        self.subscription = asyncio.create_task(self._subscribe())
        await asyncio.wait_for(self.connected.wait(), timeout=30)
        for route in ("/config", "/agent"):
            response = await self.http.get(route)
            response.raise_for_status()
        for _ in range(100):
            if self.plugin_ready.is_set() and self.connected.is_set() and not self.subscription.done():
                self.active_config = deepcopy(config)
                self.available.set()
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("runtime plugin handshake or event subscription missing")

    async def _stop(self) -> None:
        self.available.clear()
        if self.subscription:
            self.subscription.cancel()
            await asyncio.gather(self.subscription, return_exceptions=True)
            self.subscription = None
        self.connected.clear()
        await self._stop_process()
        self.plugin_ready.clear()

    async def stop(self) -> None:
        """Stop the process and subscription, preserving HTTP transport for reuse."""
        async with self._lifecycle_lock:
            await self._stop()

    async def close(self) -> None:
        await self.stop()
        await self.http.aclose()

    async def reset(self) -> None:
        """Reload configuration; the adapter must quiesce business work first."""
        async with self._lifecycle_lock:
            if self._on_event is None:
                raise RuntimeError("OpenCode has not been started")
            try:
                config = self.server_config()
            except Exception:
                raise RuntimeError("OpenCode configuration could not be loaded") from None
            previous = self.active_config
            try:
                await self._stop()
                try:
                    await self._start(config)
                except Exception:
                    log.warning("OpenCode reset failed; restoring previous configuration")
                    await self._stop()
                    if previous is None:
                        raise RuntimeError("OpenCode reset failed; bucket remains unavailable") from None
                    try:
                        await self._start(previous)
                    except Exception:
                        await self._stop()
                        raise RuntimeError("OpenCode reset and rollback failed; bucket remains unavailable") from None
                    raise RuntimeError("OpenCode reset failed; previous configuration restored") from None
            except asyncio.CancelledError:
                await self._stop()
                raise

    async def watch(self) -> None:
        """Planned process replacement does not terminate the bucket runtime."""
        while True:
            await self.available.wait()
            process = self.process
            exited = asyncio.create_task(process.wait())
            try:
                watched = [exited, self.subscription] if self.subscription else [exited]
                await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
                await self.available.wait()
                if process is self.process:
                    raise RuntimeError("OpenCode process or event subscription exited unexpectedly")
            finally:
                exited.cancel()
                await asyncio.gather(exited, return_exceptions=True)

    async def _subscribe(self) -> None:
        """Own transport reconnects; business backpressure/attribution is in the adapter."""
        while True:
            try:
                async for frame in self.events():
                    kind = frame["payload"]["type"]
                    if kind == "server.connected":
                        self.connected.set()
                    if kind != "server.heartbeat":
                        await self._on_event(frame)
                log.warning("OpenCode SSE ended; reconnecting (missed events cannot be replayed)")
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                log.warning("OpenCode SSE disconnected (%s); reconnecting (missed events cannot be replayed)",
                            type(exc).__name__)
            finally:
                self.connected.clear()
            await asyncio.sleep(1)

    def server_config(self) -> dict:
        raw = json.loads(self.config.opencode.config_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("OpenCode config must be an object")
        skills = raw.get("skills") or {}
        if "paths" in skills:
            skills["paths"] = [str(scoped_public_path(Path(path), self.config.common_root,
                self.config.frontend_id)) for path in skills["paths"]]
        return raw

    async def _start_process(self, raw: dict | None = None) -> None:
        config = self.config
        oc = config.opencode
        oc.context_root.mkdir(parents=True, exist_ok=True)
        if not oc.binary.is_file() or not os.access(oc.binary, os.X_OK):
            raise RuntimeError(f"OpenCode executable missing or not executable: {oc.binary}")
        if not oc.plugin_file.is_file():
            raise RuntimeError(f"mounted runtime plugin missing: {oc.plugin_file}")
        raw = deepcopy(self.server_config() if raw is None else raw)
        plugin = oc.plugin_file.resolve().as_uri()
        plugins = raw.setdefault("plugin", [])
        if plugin not in plugins:
            plugins.append(plugin)
        home = config.state_root / "opencode-home"
        home.mkdir(parents=True, exist_ok=True)
        # v1.18.31 checks the global config's package lock even for a bundled local plugin.
        # Seed bundled plugin dependencies; other configured plugins use OpenCode loading.
        global_config = home / "config" / "opencode"
        global_config.mkdir(parents=True, exist_ok=True)
        for name in ["package.json", "package-lock.json"]:
            shutil.copyfile(oc.dependency_dir / name, global_config / name)
        modules = global_config / "node_modules"
        if not (oc.dependency_dir / "node_modules/@opencode-ai/plugin/package.json").is_file():
            raise RuntimeError("offline plugin dependencies missing")
        target = (oc.dependency_dir / "node_modules").resolve()
        if (modules.exists() or modules.is_symlink()) and modules.resolve() != target:
            raise RuntimeError("plugin dependency directory does not match configured source")
        if not modules.exists():
            modules.symlink_to(target, target_is_directory=True)
        env = {key: os.environ[key] for key in ["PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"]
               if key in os.environ}
        env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
                    "XDG_DATA_HOME": str(home / "data"), "XDG_CACHE_HOME": str(home / "cache"),
                    "XDG_STATE_HOME": str(home / "state"), "OPENCODE_CONFIG_CONTENT": json.dumps(raw),
                    "OPENCODE_SERVER_PASSWORD": self.password, "OPENCODE_SERVER_USERNAME": "opencode",
                    "BUCKET_RPC_URL": f"http://127.0.0.1:{config.rpc_port}",
                    "BUCKET_RPC_TOKEN": self.rpc_token})
        self.process = await asyncio.create_subprocess_exec(str(oc.binary), "serve", "--hostname",
            "127.0.0.1", "--port", str(oc.port), cwd=oc.context_root, env=env,
            stdin=asyncio.subprocess.DEVNULL)
        for _ in range(60):
            if self.process.returncode is not None:
                raise RuntimeError("OpenCode exited during startup")
            try:
                if await self.health():
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)
        await self._stop_process()
        raise RuntimeError("OpenCode startup timed out")

    async def _stop_process(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 10)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()


    async def health(self) -> bool:
        response = await self.http.get("/global/health")
        response.raise_for_status()
        data = response.json()
        return bool(data.get("healthy"))

    async def events(self):
        """One global stream covers every workspace on this OpenCode server.

        OpenCode has no replay contract here; reconnecting starts a live stream.
        Dispatch only complete SSE frames, including multi-line JSON data.
        """
        async with self.http.stream("GET", "/global/event",
                headers={"Accept": "text/event-stream"},
                timeout=httpx.Timeout(30, read=45)) as response:
            response.raise_for_status()
            if response.headers.get("content-type", "").split(";", 1)[0] != "text/event-stream":
                raise ValueError("OpenCode did not return an event stream")
            data = []
            async for line in response.aiter_lines():
                if not line:
                    if data:
                        yield json.loads("\n".join(data))
                        data.clear()
                elif line.startswith("data:"):
                    value = line[5:]
                    data.append(value[1:] if value.startswith(" ") else value)

    async def create_session(self, permissions: list[dict], context: dict, *, directory: Path | None = None) -> str:
        response = await self.http.post("/session", params=self._params(directory), json={"title": "Managed conversation",
            "permission": permissions, "metadata": {"bucket_runtime": context}})
        response.raise_for_status()
        data = response.json()
        if Path(data["directory"]).resolve() != (directory or self.config.opencode.context_root).resolve():
            raise RuntimeError("OpenCode created session in an unexpected directory")
        self.check_permissions(await self.session(data["id"], directory=directory), permissions)
        return data["id"]

    async def set_context(self, session_id: str, context: dict, info: dict, *, directory: Path | None = None) -> None:
        # PATCH only metadata: resending permissions appends rules in OpenCode 1.18.31.
        metadata = dict(info["metadata"])
        previous = metadata["bucket_runtime"]
        if previous["schema_version"] != 1:
            raise ValueError("OpenCode session metadata schema mismatch")
        for field in ("frontend_id", "user_id", "identity_id", "chat_id"):
            if previous[field] != context[field]:
                raise PermissionError("OpenCode session metadata ownership mismatch")
        requests = {**previous["requests"], **context["requests"]}
        metadata["bucket_runtime"] = {**context, "requests": dict(list(requests.items())[-128:])}
        response = await self.http.patch(f"/session/{session_id}", params=self._params(directory), json={"metadata": metadata})
        response.raise_for_status()

    @staticmethod
    def check_permissions(info: dict, permissions: list[dict]) -> None:
        # v1.18.31 PATCH appends rules. Managed sessions have an immutable policy;
        # never silently accumulate or repair an unknown policy on a shared server.
        if info["permission"] != permissions:
            raise RuntimeError("session permission readback failed: " + json.dumps(info["permission"]))

    async def submit(self, session_id: str, message_id: str, text: str, *, directory: Path | None = None) -> None:
        # Resolve the effective config for this workspace on every turn, including resumed sessions.
        try:
            config = await self.http.get("/config", params=self._params(directory))
        except httpx.TransportError as exc:
            # No prompt was sent: do not enter runtime's ambiguous-submission wait path.
            raise RuntimeError("Unable to read OpenCode default model before submission") from exc
        config.raise_for_status()
        model = config.json().get("model")
        if not isinstance(model, str) or "/" not in model:
            raise RuntimeError("OpenCode default model must be configured as provider/model")
        provider_id, model_id = model.split("/", 1)
        if not provider_id.strip() or not model_id.strip():
            raise RuntimeError("OpenCode default model must be configured as provider/model")
        response = await self.http.post(f"/session/{session_id}/prompt_async", params=self._params(directory), json={
            "messageID": message_id,
            "model": {"providerID": provider_id, "modelID": model_id},
            "parts": [{"type": "text", "text": text}]})
        response.raise_for_status()

    async def paths(self, *, directory: Path | None = None) -> dict:
        response = await self.http.get("/path", params=self._params(directory))
        response.raise_for_status()
        return response.json()

    async def session(self, session_id: str, *, directory: Path | None = None) -> dict:
        response = await self.http.get(f"/session/{session_id}", params=self._params(directory))
        response.raise_for_status()
        return response.json()

    async def abort(self, session_id: str, *, directory: Path | None = None) -> None:
        response = await self.http.post(f"/session/{session_id}/abort", params=self._params(directory))
        response.raise_for_status()

    async def delete_session(self, session_id: str, *, directory: Path | None = None) -> None:
        response = await self.http.delete(f"/session/{session_id}", params=self._params(directory))
        if response.status_code != 404:
            response.raise_for_status()

    async def children(self, session_id: str, *, directory: Path | None = None) -> list[dict]:
        response = await self.http.get(f"/session/{session_id}/children", params=self._params(directory))
        response.raise_for_status()
        return response.json()
