from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

import httpx

from agent_backend.config import OpenCodeConfig
from agent_backend.models import Usage

log = logging.getLogger(__name__)


class OpenCodeClient:
    def __init__(self, config: OpenCodeConfig, password: str):
        self.config = config
        self.sse_connected = False
        self.http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{config.port}",
            auth=("opencode", password), timeout=30, trust_env=False,
            params={"directory": str(config.context_root)})

    async def health(self) -> bool:
        response = await self.http.get("/global/health")
        response.raise_for_status()
        data = response.json()
        return bool(data.get("healthy"))

    async def create_session(self, permissions: list[dict]) -> str:
        response = await self.http.post("/session", json={"title": "Managed conversation", "permission": permissions})
        response.raise_for_status()
        data = response.json()
        await self.set_permissions(data["id"], permissions)
        return data["id"]

    async def set_permissions(self, session_id: str, permissions: list[dict]) -> None:
        # v1.18.31 PATCH appends rules. Managed sessions have an immutable policy;
        # never silently accumulate or repair an unknown policy on a shared server.
        current = await self.http.get(f"/session/{session_id}")
        current.raise_for_status()
        if current.json().get("permission") != permissions:
            raise RuntimeError("session permission readback failed: " + json.dumps(current.json().get("permission")))

    async def submit(self, session_id: str, message_id: str, text: str, model: str) -> None:
        provider, model_id = model.split("/", 1)
        response = await self.http.post(f"/session/{session_id}/prompt_async", json={
            "messageID": message_id, "model": {"providerID": provider, "modelID": model_id},
            "agent": "build", "parts": [{"type": "text", "text": text}]})
        response.raise_for_status()

    async def messages(self, session_id: str) -> list[dict]:
        response = await self.http.get(f"/session/{session_id}/message")
        response.raise_for_status()
        return response.json()

    async def paths(self) -> dict:
        response = await self.http.get("/path")
        response.raise_for_status()
        return response.json()

    async def session(self, session_id: str) -> dict:
        response = await self.http.get(f"/session/{session_id}")
        response.raise_for_status()
        return response.json()

    async def abort(self, session_id: str) -> None:
        response = await self.http.post(f"/session/{session_id}/abort")
        response.raise_for_status()

    async def events(self) -> AsyncIterator[dict]:
        while True:
            try:
                async with self.http.stream("GET", "/global/event", timeout=httpx.Timeout(40, read=45)) as response:
                    response.raise_for_status()
                    self.sse_connected = True
                    lines = []
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            lines.append(line[5:].lstrip())
                        elif not line and lines:
                            data = json.loads("\n".join(lines))
                            lines.clear()
                            yield data.get("payload", data)
            except (httpx.HTTPError, ValueError):
                log.warning("OpenCode event stream disconnected; reconciling snapshots")
                await asyncio.sleep(1)
            finally:
                self.sse_connected = False

    async def close(self):
        await self.http.aclose()


def final_message(messages: list[dict], input_id: str) -> tuple[str, str | None] | None:
    """Only a completed assistant answer correlated to this input may terminate a run."""
    answers = [m for m in messages if m["info"].get("role") == "assistant"
               and m["info"].get("parentID") == input_id]
    if not answers:
        return None
    if any(p.get("type") == "tool" and p.get("state", {}).get("status") in {"pending", "running"}
           for m in answers for p in m.get("parts", [])):
        return None
    last = max(answers, key=lambda m: (m["info"].get("time", {}).get("created", 0), m["info"]["id"]))
    info = last["info"]
    if not info.get("time", {}).get("completed"):
        return None
    if info.get("error"):
        return "", str(info["error"].get("name", "MODEL_ERROR"))
    if info.get("finish") not in {"stop", "end_turn", "length", "content-filter"}:
        return None
    text = "\n".join(p["text"] for p in last.get("parts", []) if p.get("type") == "text"
                     and not p.get("ignored") and p.get("text"))
    return text, None if info["finish"] in {"stop", "end_turn"} else "MODEL_" + info["finish"].upper()


def usage_for(messages: list[dict], input_id: str) -> Usage:
    result = Usage()
    for message in messages:
        info = message["info"]
        if info.get("role") != "assistant" or info.get("parentID") != input_id:
            continue
        tokens = info.get("tokens", {})
        result.input_tokens += int(tokens.get("input", 0))
        result.output_tokens += int(tokens.get("output", 0))
        result.reasoning_tokens += int(tokens.get("reasoning", 0))
        result.cache_read_tokens += int(tokens.get("cache", {}).get("read", 0))
        result.cache_write_tokens += int(tokens.get("cache", {}).get("write", 0))
    return result
