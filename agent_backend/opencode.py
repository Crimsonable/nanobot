"""Small HTTP client for the pinned native OpenCode API; no execution engine."""
from __future__ import annotations

from pathlib import Path

import httpx

from agent_backend.native_sse import messages


class OpenCode:
    def __init__(self, endpoint: dict):
        self.http = httpx.AsyncClient(base_url=endpoint["url"],
            auth=("opencode", endpoint["password"]), timeout=30, trust_env=False)

    async def close(self):
        await self.http.aclose()

    async def call(self, method, path, **kwargs):
        response = await self.http.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    async def ready(self):
        info = await self.call("GET", "/api/info")
        if info["version"].removeprefix("v") != "2.3.2":
            raise RuntimeError("OpenCode 2.3.2 is required")
        state = await self.call("GET", "/api/experimental/migration/v1")
        if state["status"] == "error":
            raise RuntimeError("OpenCode storage migration failed")
        return state["status"] == "completed"

    async def session(self, session_id):
        return (await self.call("GET", f"/api/session/{session_id}"))["data"]

    async def create_session(self, session_id, directory, owner):
        try:
            info = await self.session(session_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            info = (await self.call("POST", "/api/session", json={"id": session_id,
                "location": {"directory": str(directory)}, "title": "Managed conversation",
                "metadata": {"managed": owner},
                # The plugin evaluates the mounted policy for each tool invocation.
                "permissions": [{"action": "*", "resource": "*", "effect": "allow"}]}))["data"]
        if info.get("metadata", {}).get("managed") != owner or Path(info["location"]["directory"]) != directory:
            raise PermissionError("native session ownership mismatch")
        return info

    async def submit(self, native, text, metadata):
        sid, mid = native["session_id"], native["message_id"]
        # Native inbox IDs are durable idempotency keys, including after delivery.
        response = await self.http.post(f"/api/session/{sid}/prompt", json={
            "id": mid, "text": text, "metadata": {"managed": metadata}, "delivery": "queue"})
        if response.status_code == 409:
            pending = await self.inbox(sid)
            existing = next((p.get("payload", {}) for p in pending if p["id"] == mid), None)
            if existing is None:
                timeline = await messages(self.http, sid, message_id=mid)
                existing = timeline[0] if timeline else None
            if existing and existing.get("metadata", {}).get("managed") == metadata:
                return
        response.raise_for_status()

    async def inbox(self, session_id):
        return (await self.call("GET", f"/api/session/{session_id}/inbox"))["data"]

    async def children(self, session_id):
        result, params = [], {"parentID": session_id, "limit": 100}
        while True:
            page = await self.call("GET", "/api/session", params=params)
            result.extend(page["data"])
            cursor = page.get("cursor", {}).get("next")
            if not cursor:
                return result
            params["cursor"] = cursor

    async def tree(self, session_id, *, missing_ok=False):
        try:
            result = [await self.session(session_id)]
        except httpx.HTTPStatusError as exc:
            if missing_ok and exc.response.status_code == 404:
                return []
            raise
        seen = {session_id}
        for info in result:
            for child in await self.children(info["id"]):
                if child["id"] not in seen:
                    result.append(child)
                    seen.add(child["id"])
        return result

    async def idle(self, sessions):
        active = (await self.call("GET", "/api/session/active"))["data"]
        if any(info["id"] in active for info in sessions):
            return False
        for info in sessions:
            if await self.inbox(info["id"]):
                return False
        if not sessions:
            return True
        jobs = await self.call("GET", "/api/experimental/job", params={
            "sessionID": sessions[0]["id"], "descendants": "true"})
        return not jobs["data"]

    async def interrupt(self, session_id):
        await self.call("POST", f"/api/session/{session_id}/interrupt")
        await self.call("POST", f"/api/experimental/session/{session_id}/wait")

    async def cancel_input(self, native):
        sid, mid = native["session_id"], native["message_id"]
        await self.call("DELETE", f"/api/session/{sid}/inbox/{mid}")
        timeline = await messages(self.http, sid, message_id=mid)
        # A subsequent native user turn already owns the foreground execution.
        if not timeline or any(m["type"] == "user" for m in timeline[1:]):
            return
        for info in reversed(await self.tree(sid)):
            await self.interrupt(info["id"])

    async def export_session(self, session_id):
        return (await self.call("GET", f"/api/experimental/session/{session_id}/export"))["data"]

    async def import_session(self, data, directory):
        response = await self.http.post("/api/experimental/session/import",
            json={**data, "location": {"directory": str(directory)}})
        if response.status_code != 409:
            response.raise_for_status()

    async def delete_session(self, session_id):
        response = await self.http.delete(f"/api/session/{session_id}")
        if response.status_code != 404:
            response.raise_for_status()
