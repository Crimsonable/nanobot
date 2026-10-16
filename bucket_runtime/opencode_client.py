from __future__ import annotations

import json
from copy import copy
from pathlib import Path

import httpx

from agent_backend.config import OpenCodeConfig

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
        self.check_permissions(await self.session(data["id"]), permissions)
        return data["id"]

    async def set_context(self, session_id: str, context: dict, info: dict) -> None:
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
        response = await self.http.patch(f"/session/{session_id}", params=self.params, json={"metadata": metadata})
        response.raise_for_status()

    @staticmethod
    def check_permissions(info: dict, permissions: list[dict]) -> None:
        # v1.18.31 PATCH appends rules. Managed sessions have an immutable policy;
        # never silently accumulate or repair an unknown policy on a shared server.
        if info["permission"] != permissions:
            raise RuntimeError("session permission readback failed: " + json.dumps(info["permission"]))

    async def submit(self, session_id: str, message_id: str, text: str) -> None:
        # Resolve the effective config for this workspace on every turn, including resumed sessions.
        try:
            config = await self.http.get("/config", params=self.params)
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
        response = await self.http.post(f"/session/{session_id}/prompt_async", params=self.params, json={
            "messageID": message_id,
            "model": {"providerID": provider_id, "modelID": model_id},
            "parts": [{"type": "text", "text": text}]})
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

    async def delete_session(self, session_id: str) -> None:
        response = await self.http.delete(f"/session/{session_id}", params=self.params)
        if response.status_code != 404:
            response.raise_for_status()

    async def children(self, session_id: str) -> list[dict]:
        response = await self.http.get(f"/session/{session_id}/children", params=self.params)
        response.raise_for_status()
        return response.json()

    async def close(self):
        await self.http.aclose()
