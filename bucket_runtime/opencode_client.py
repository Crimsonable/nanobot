from __future__ import annotations

import json
from copy import copy
from pathlib import Path

import httpx

from agent_backend.config import OpenCodeConfig
from agent_backend.models import digest

class OpenCodeClient:
    def __init__(self, config: OpenCodeConfig, password: str):
        self.config = config
        self.directory = config.context_root.resolve()
        self.http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{config.port}",
            auth=("opencode", password), timeout=30, trust_env=False,
            params={"directory": str(config.context_root)})

    def for_directory(self, directory: Path) -> OpenCodeClient:
        """A request-local view sharing the server transport, never its mutable defaults."""
        scoped = copy(self)
        scoped.directory = directory.resolve()
        return scoped

    @property
    def params(self) -> dict[str, str]:
        return {"directory": str(self.directory)}

    async def health(self) -> bool:
        response = await self.http.get("/global/health")
        response.raise_for_status()
        data = response.json()
        return bool(data.get("healthy"))

    async def create_session(self, permissions: list[dict], context: dict) -> str:
        response = await self.http.post("/session", params=self.params, json={"title": "Managed conversation",
            "permission": permissions, "metadata": {"bucket_runtime": context}})
        response.raise_for_status()
        data = response.json()
        if Path(data["directory"]).resolve() != self.directory:
            raise RuntimeError("OpenCode created session in an unexpected directory")
        await self.set_permissions(data["id"], permissions)
        return data["id"]

    async def set_context(self, session_id: str, context: dict) -> None:
        # PATCH only metadata: resending permissions appends rules in OpenCode 1.18.31.
        info = await self.session(session_id)
        metadata = dict(info.get("metadata") or {})
        previous = metadata.get("bucket_runtime") or {}
        for field in ("frontend_id", "user_id", "identity_id", "chat_id"):
            if field in previous and field in context and previous[field] != context[field]:
                raise PermissionError("OpenCode session metadata ownership mismatch")
        if "conversation_id" in previous and "chat_id" in context:
            if previous["conversation_id"] != digest([context["identity_id"], context["chat_id"]]):
                raise PermissionError("OpenCode session metadata ownership mismatch")
        requests = {**previous.get("requests", {}), **context["requests"]}
        metadata["bucket_runtime"] = {**context, "requests": dict(list(requests.items())[-128:])}
        response = await self.http.patch(f"/session/{session_id}", params=self.params, json={"metadata": metadata})
        response.raise_for_status()

    async def set_permissions(self, session_id: str, permissions: list[dict]) -> None:
        # v1.18.31 PATCH appends rules. Managed sessions have an immutable policy;
        # never silently accumulate or repair an unknown policy on a shared server.
        current = await self.http.get(f"/session/{session_id}", params=self.params)
        current.raise_for_status()
        if current.json().get("permission") != permissions:
            raise RuntimeError("session permission readback failed: " + json.dumps(current.json().get("permission")))

    async def submit(self, session_id: str, message_id: str, text: str, model: str) -> None:
        provider, model_id = model.split("/", 1)
        response = await self.http.post(f"/session/{session_id}/prompt_async", params=self.params, json={
            "messageID": message_id, "model": {"providerID": provider, "modelID": model_id},
            "agent": "build", "parts": [{"type": "text", "text": text}]})
        response.raise_for_status()

    async def paths(self) -> dict:
        response = await self.http.get("/path", params=self.params)
        response.raise_for_status()
        return response.json()

    async def session(self, session_id: str) -> dict:
        response = await self.http.get(f"/session/{session_id}", params=self.params)
        response.raise_for_status()
        return response.json()

    async def abort(self, session_id: str) -> None:
        response = await self.http.post(f"/session/{session_id}/abort", params=self.params)
        response.raise_for_status()

    async def close(self):
        await self.http.aclose()
