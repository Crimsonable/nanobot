"""OpenCode 2.x process, HTTP transport and native event subscription."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from agent_backend.config import RuntimeConfig

log = logging.getLogger(__name__)


class OpenCodeServer:
    def __init__(self, config: RuntimeConfig, password: str | None = None):
        self.config = config
        self.password = password or secrets.token_urlsafe(32)
        self.rpc_token = secrets.token_urlsafe(32)
        self.process: asyncio.subprocess.Process | None = None
        self.connected = asyncio.Event()
        self.plugin_ready = asyncio.Event()
        self.available = asyncio.Event()
        self.subscription: asyncio.Task | None = None
        self._on_event: Callable[[dict], Awaitable[None]] | None = None
        self.http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{config.opencode.port}",
            auth=("opencode", self.password), timeout=30, trust_env=False)

    @property
    def ready(self) -> bool:
        return self.available.is_set()

    def _params(self, directory: Path | None = None) -> dict[str, str]:
        return {"location[directory]": str((directory or self.config.opencode.context_root).resolve())}

    async def start(self, on_event: Callable[[dict], Awaitable[None]]) -> None:
        if self.process and self.process.returncode is None:
            raise RuntimeError("OpenCode is already running")
        self._on_event = on_event
        try:
            await self._start_process()
            await self._wait_for_storage()
            self.subscription = asyncio.create_task(self._subscribe())
            await asyncio.wait_for(self.connected.wait(), timeout=30)
            await self.paths()
            await asyncio.wait_for(self.plugin_ready.wait(), timeout=30)
            self.available.set()
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        self.available.clear()
        if self.subscription:
            self.subscription.cancel()
            await asyncio.gather(self.subscription, return_exceptions=True)
            self.subscription = None
        self.connected.clear()
        await self._stop_process()
        self.plugin_ready.clear()

    async def close(self) -> None:
        await self.stop()
        await self.http.aclose()

    async def watch(self) -> None:
        exited = asyncio.create_task(self.process.wait())
        try:
            await asyncio.wait([exited, self.subscription], return_when=asyncio.FIRST_COMPLETED)
            raise RuntimeError("OpenCode process or event subscription exited unexpectedly")
        finally:
            exited.cancel()
            await asyncio.gather(exited, return_exceptions=True)

    async def _subscribe(self) -> None:
        while True:
            try:
                async for event in self.events():
                    if event["type"] == "server.connected":
                        self.connected.set()
                    else:
                        await self._on_event(event)
                log.warning("OpenCode SSE ended; reconnecting (missed events cannot be replayed)")
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                log.warning("OpenCode SSE disconnected (%s); reconnecting", type(exc).__name__)
            finally:
                self.connected.clear()
            await asyncio.sleep(1)

    async def _start_process(self) -> None:
        config, oc = self.config, self.config.opencode
        oc.context_root.mkdir(parents=True, exist_ok=True)
        for path in (oc.binary, oc.plugin_dir / "package.json", oc.plugin_dir / "dist/index.mjs", oc.config_file):
            if not path.is_file():
                raise RuntimeError(f"OpenCode runtime file missing: {path}")
        if not os.access(oc.binary, os.X_OK):
            raise RuntimeError("OpenCode binary is not executable")
        home = config.state_root / "opencode-home"
        home.mkdir(parents=True, exist_ok=True)
        env = {key: os.environ[key] for key in ["PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"]
               if key in os.environ}
        env.update({"HOME": str(home), "USERPROFILE": str(home), "XDG_CONFIG_HOME": str(home / "config"),
                    "XDG_DATA_HOME": str(home / "data"), "XDG_CACHE_HOME": str(home / "cache"),
                    "XDG_STATE_HOME": str(home / "state"), "OPENCODE_PASSWORD": self.password,
                    # Pass the actual file to the native watcher. Only the trusted plugin is injected.
                    "OPENCODE_CONFIG": str(oc.config_file.resolve()),
                    "OPENCODE_CONFIG_PROJECT_DISABLE": "true",
                    "OPENCODE_CONFIG_CONTENT": json.dumps({"plugins": [oc.plugin_dir.resolve().as_uri()]}),
                    "BUCKET_PUBLIC_ROOT": str((config.common_root / config.frontend_id / "app").resolve()),
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
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
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
        response = await self.http.get("/api/info")
        response.raise_for_status()
        version = response.json()["version"].removeprefix("v")
        if version.split(".")[0] != "2":
            raise RuntimeError("OpenCode 2.x is required")
        return True

    async def _wait_for_storage(self) -> None:
        # V2 starts its native database upgrade in the background. Do not expose
        # sessions or allow cross-bucket copies until that upgrade has finished.
        async with asyncio.timeout(self.config.limits.request_seconds):
            while True:
                response = await self.http.get("/api/experimental/migration/v1")
                response.raise_for_status()
                state = response.json()
                if state["status"] == "completed":
                    return
                if state["status"] == "error":
                    raise RuntimeError("OpenCode storage migration failed: " + state["error"])
                await asyncio.sleep(0.5)

    async def events(self):
        """The native V2 stream is global, live-only, with JSON data and comment heartbeats."""
        async with self.http.stream("GET", "/api/event", headers={"Accept": "text/event-stream"},
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
                    data.append(line[5:].removeprefix(" "))

    async def create_session(self, permissions: list[dict], context: dict, *, directory: Path | None = None) -> str:
        directory = (directory or self.config.opencode.context_root).resolve()
        response = await self.http.post("/api/session", json={"title": "Managed conversation",
            "location": {"directory": str(directory)}, "permissions": permissions,
            "metadata": {"bucket_runtime": context}})
        response.raise_for_status()
        info = response.json()["data"]
        if Path(info["location"]["directory"]).resolve() != directory:
            raise RuntimeError("OpenCode created session in an unexpected directory")
        self.check_permissions(info, permissions)
        return info["id"]

    async def set_context(self, session_id: str, context: dict, info: dict, *, permissions: list[dict]) -> None:
        metadata = dict(info["metadata"])
        previous = metadata["bucket_runtime"]
        if previous["schema_version"] != 1:
            raise ValueError("OpenCode session metadata schema mismatch")
        for field in ("frontend_id", "user_id", "identity_id", "chat_id"):
            if previous[field] != context[field]:
                raise PermissionError("OpenCode session metadata ownership mismatch")
        requests = {**previous["requests"], **context["requests"]}
        metadata["bucket_runtime"] = {**context, "requests": dict(list(requests.items())[-128:])}
        response = await self.http.patch(f"/api/session/{session_id}",
                                        json={"metadata": metadata, "permissions": permissions})
        response.raise_for_status()
        self.check_permissions(await self.session(session_id), permissions)

    @staticmethod
    def check_permissions(info: dict, permissions: list[dict]) -> None:
        if info.get("permissions") != permissions:
            raise RuntimeError("session permission readback failed")

    async def submit(self, session_id: str, message_id: str, text: str, *, directory: Path | None = None) -> None:
        # Read the settled model registry, including config/plugin transforms, on every turn.
        try:
            response = await self.http.get("/api/model/default", params=self._params(directory))
            response.raise_for_status()
            model = response.json()["data"]
            selected = {"providerID": model["providerID"], "id": model["id"]}
            response = await self.http.post(f"/api/session/{session_id}/model", json={"model": selected})
            response.raise_for_status()
        except (httpx.HTTPError, KeyError, TypeError) as exc:
            raise RuntimeError("Unable to select OpenCode default model before submission") from exc
        response = await self.http.post(f"/api/session/{session_id}/prompt",
            json={"id": message_id, "text": text, "delivery": "queue"})
        response.raise_for_status()

    async def paths(self, *, directory: Path | None = None) -> dict:
        response = await self.http.get("/api/location", params=self._params(directory))
        response.raise_for_status()
        return response.json()

    async def session(self, session_id: str, *, directory: Path | None = None) -> dict:
        response = await self.http.get(f"/api/session/{session_id}")
        response.raise_for_status()
        return response.json()["data"]

    async def export_session(self, session_id: str) -> dict:
        response = await self.http.get(f"/api/experimental/session/{session_id}/export")
        response.raise_for_status()
        return response.json()["data"]

    async def import_session(self, data: dict, *, directory: Path) -> None:
        response = await self.http.post("/api/experimental/session/import",
                                       json={**data, "location": {"directory": str(directory)}})
        # A lost reply is retried by the caller, which verifies the entire transcript.
        if response.status_code != 409:
            response.raise_for_status()

    async def session_tree(self, session_id: str) -> list[dict]:
        sessions = [await self.session(session_id)]
        seen = {session_id}
        for info in sessions:
            for child in await self.children(info["id"]):
                if child["id"] not in seen:
                    sessions.append(child)
                    seen.add(child["id"])
        return sessions

    async def sessions_idle(self, sessions: list[dict]) -> bool:
        response = await self.http.get("/api/session/active")
        response.raise_for_status()
        if any(info["id"] in response.json()["data"] for info in sessions):
            return False
        for info in sessions:
            response = await self.http.get(f"/api/session/{info['id']}/inbox")
            response.raise_for_status()
            if response.json()["data"]:
                return False
        response = await self.http.get("/api/experimental/job", params={
            "sessionID": sessions[0]["id"], "descendants": "true"})
        response.raise_for_status()
        # Includes completed jobs whose results have not yet been consumed.
        return not response.json()["data"]

    async def abort(self, session_id: str, *, directory: Path | None = None) -> None:
        """Interrupt and await this session's execution cleanup in the native process."""
        response = await self.http.post(f"/api/session/{session_id}/interrupt")
        response.raise_for_status()
        # Even interrupted=false can mean an earlier interruption is still settling.
        # Keep the transport timeout: callers must not proceed if idle is unconfirmed.
        response = await self.http.post(f"/api/experimental/session/{session_id}/wait")
        response.raise_for_status()

    async def delete_session(self, session_id: str, *, directory: Path | None = None) -> None:
        response = await self.http.delete(f"/api/session/{session_id}")
        if response.status_code != 404:
            response.raise_for_status()

    async def children(self, session_id: str, *, directory: Path | None = None) -> list[dict]:
        result, params = [], {"parentID": session_id, "limit": 100}
        while True:
            response = await self.http.get("/api/session", params=params)
            response.raise_for_status()
            page = response.json()
            result.extend(page["data"])
            cursor = page["cursor"].get("next")
            if not cursor:
                return result
            params["cursor"] = cursor
