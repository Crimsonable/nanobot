"""Gateway-owned native SSE consumption, durable replay and reply projection.

Global SSE supplies live events. Native projected messages recover complete
content after reconnects; stock OpenCode serve does not persist event payloads.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque

import httpx

from agent_backend.models import Result, digest
from agent_backend.native_sse import messages, sse
from agent_backend.opencode import OpenCode
from channel_gateway.legacy_events import part_id, translate
from channel_gateway.native_reply import accumulate, result_for, snapshot_events
from channel_gateway.session_logger import SessionLogger

log = logging.getLogger(__name__)


class NativeGateway:
    def __init__(self, service):
        self.service, self.db = service, service.db
        self.tasks = {}
        self.connections = {}
        self.connection_locks = {}
        self.loggers = {}

    def watch(self, request_id):
        if request_id not in self.tasks:
            task = asyncio.create_task(self.run(request_id))
            self.tasks[request_id] = task
            task.add_done_callback(lambda done: self.tasks.pop(request_id, None))

    async def connection(self, bucket_id):
        async with self.connection_locks.setdefault(bucket_id, asyncio.Lock()):
            return await self.connect(bucket_id)

    async def connect(self, bucket_id):
        bucket = await self.service.control.bucket(bucket_id)
        endpoint = bucket.get("opencode")
        if not endpoint:
            raise ConnectionError("OpenCode endpoint not registered")
        current = self.connections.get(bucket_id)
        if current and current["endpoint"] == endpoint and not current["task"].done():
            return current
        if current:
            current["task"].cancel()
            await asyncio.gather(current["task"], return_exceptions=True)
            await current["http"].aclose()
        native = OpenCode(endpoint)
        current = {"endpoint": endpoint, "native": native, "http": native.http, "events": deque(maxlen=4096)}
        current["task"] = asyncio.create_task(self.live(bucket_id, current))
        self.connections[bucket_id] = current
        return current

    async def live(self, bucket_id, connection):
        while True:
            try:
                async with connection["http"].stream("GET", "/api/event",
                        timeout=httpx.Timeout(30, read=45)) as response:
                    async for event in sse(response):
                        if event["type"] != "server.connected":
                            connection["events"].append(event)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                log.warning("native live stream reconnecting bucket_id=%s", bucket_id)
            await asyncio.sleep(1)

    def ingest(self, row, state, session_id, event):
        key = digest([row["bucket_id"], row["request_id"], event["id"]])
        if self.db.seen("native", key):
            return
        native = row["native"]
        projection = state.setdefault("projections", {}).setdefault(session_id, {})
        projection["directory"] = native["directory"]
        if row["input"].get("stream"):
            for legacy in translate(event, projection):
                self.service.streams.append(row["request_id"], "opencode.event", legacy)
        frontend = row["frontend_id"]
        if frontend not in self.loggers:
            runtime = self.service.config.bucket_templates.get(frontend)
            path = runtime.logging_file if runtime else None
            if path:
                from pathlib import Path
                text = path.as_posix()
                if text.startswith("/mnt/common/{frontend_id}/"):
                    text = runtime.common_root.as_posix() + text[len("/mnt/common"):]
                path = Path(text.replace("{frontend_id}", frontend))
            self.loggers[frontend] = SessionLogger(path)
        line = self.loggers[frontend].format(event, {
            **row["submit"]["identity"], "chat_id": row["chat_id"]})
        if line:
            data = event["data"]
            line_key = digest([row["request_id"], session_id, event["type"],
                               data.get("assistantMessageID"), data.get("ordinal"), data.get("id")])
            if not self.db.seen("session_log", line_key):
                self.loggers[frontend].write(line)
                self.db.accept("session_log", line_key)
        self.db.accept("native", key)

    async def replay(self, row, connection):
        native, client = row["native"], connection["http"]
        state = self.db.get("native_reply", row["request_id"]) or {}
        history = await messages(client, native["session_id"], message_id=native["message_id"])
        if not history:
            return state
        boundary = next((i for i, m in enumerate(history[1:], 1) if m["type"] == "user"), len(history))
        end = next((m["time"]["created"] for m in history[boundary:] if m["type"] == "assistant"), float("inf"))
        has_next = boundary < len(history)
        history = history[:boundary]
        start = next((m["time"]["created"] for m in history if m["type"] == "assistant"), history[0]["time"]["created"])
        tree = await connection["native"].tree(native["session_id"])
        sessions = [info["id"] for info in tree]
        state["sessions"] = sessions
        state["settled"] = has_next or (history[-1]["type"] == "idle" and await connection["native"].idle(tree))
        state["outcome"] = history[-1] if history[-1]["type"] == "idle" else None
        if has_next and not state["outcome"]:
            # The native drain can deliver the next queued input without an idle
            # marker. Delivery is the authoritative boundary between these replies.
            last = next((m for m in reversed(history) if m["type"] == "assistant"), {})
            error = last.get("error")
            state["outcome"] = {"outcome": "failed" if error else "succeeded", "error": error}
        assistants = {m["id"] for m in history if m["type"] == "assistant"}
        # Drain native live events first. Full message snapshots below repair any
        # events missed during a reconnect, including long outputs and tool results.
        for event in list(connection["events"]):
            data = event.get("data", {})
            sid = data.get("sessionID")
            if sid not in sessions or not start <= event.get("created", 0) < end:
                continue
            if sid == native["session_id"] and data.get("assistantMessageID") and data["assistantMessageID"] not in assistants:
                continue
            if sid == native["session_id"] and data.get("inboxID") and data["inboxID"] != native["message_id"]:
                continue
            if event["created"] <= state.get("snapshot_through", {}).get(sid, -1):
                continue
            projection = state.get("projections", {}).get(sid, {})
            if event["type"] in {"session.text.delta", "session.reasoning.delta"}:
                category = event["type"].split(".")[1]
                pid = part_id(f"{data['assistantMessageID']}:{category}:{data['ordinal']}")
                part = projection.get("parts", {}).get(pid)
                if not part or part.get("time", {}).get("end") is not None:
                    continue
            with self.db.transaction():
                self.ingest(row, state, sid, event)
                self.db.put("native_reply", row["request_id"], state)
        reply = {}
        for sid in sessions:
            timeline = history if sid == native["session_id"] else [m for m in
                await messages(client, sid, since=start) if start <= m["time"]["created"] < end]
            for event in snapshot_events(sid, timeline):
                if sid == native["session_id"]:
                    accumulate(reply, event)
                with self.db.transaction():
                    self.ingest(row, state, sid, event)
                    self.db.put("native_reply", row["request_id"], state)
            through = max((m["time"].get("completed", m["time"]["created"] if m["type"] == "idle" else -1)
                           for m in timeline), default=-1)
            state.setdefault("snapshot_through", {})[sid] = through
        # Usage is recomputed from the authoritative snapshot, never added twice
        # when an event and its recovered snapshot describe the same native step.
        state.update(steps=reply.get("steps", {}), usage=reply.get("usage", {}), execution=reply.get("execution"))
        if has_next:
            state["execution"] = "session.execution." + state["outcome"]["outcome"]
        self.db.put("native_reply", row["request_id"], state)
        return state

    async def poll(self, request_id):
        row = self.db.get("request", request_id)
        if row.get("result"):
            if not row.get("output_collected"):
                await self.service.control.collected(row["frontend_id"], request_id,
                    {key: row["result"].get(key) for key in ("request_id", "status", "error")})
                self.db.put("request", request_id, {**self.db.get("request", request_id), "output_collected": True})
            return True
        remote = await self.service.control.request(row["frontend_id"], request_id)
        row = {**self.db.get("request", request_id), "native": remote.get("native"),
               "status": remote["status"] if not remote.get("result") else row["status"], "submit": remote["submit"]}
        self.db.put("request", request_id, row)
        terminal = remote.get("result")
        missing = terminal and terminal.get("error") == "BUCKET_MISSING_EXECUTION_INTERRUPTED"
        state = {}
        if row.get("native") and not missing:
            state = await self.replay(row, await self.connection(row["bucket_id"]))
        if not terminal and state.get("settled"):
            # Cancellation/deadline handling may have completed during native reads.
            remote = await self.service.control.request(row["frontend_id"], request_id)
            terminal = remote.get("result")
            if remote.get("cancel_requested") and not terminal:
                return False
        if not terminal and state.get("settled"):
            outcome = state["outcome"]
            error = outcome.get("error")
            terminal = {"request_id": request_id, "status": {"succeeded": "completed", "failed": "failed",
                "interrupted": "cancelled"}[outcome["outcome"]],
                "error": error.get("message", str(error)) if isinstance(error, dict) else error}
        if terminal:
            attachments = self.service.artifacts.attachments(request_id)
            if row["input"]["content"].strip().lower() == "/cancel" and terminal["status"] == "completed":
                result = Result(**terminal, content="停止请求已处理。")
            elif row.get("native") and not missing:
                result = result_for(terminal, state, attachments)
            else:
                result = Result(**terminal, attachments=attachments, partial=bool(attachments))
            self.service.finish(request_id, result)
        return False

    async def run(self, request_id):
        try:
            while True:
                try:
                    if await self.poll(request_id):
                        return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("native output pending request_id=%s", request_id, exc_info=True)
                await asyncio.sleep(0.5)
        finally:
            row = self.db.get("request", request_id)
            bucket_id = row["bucket_id"]
            async with self.connection_locks.setdefault(bucket_id, asyncio.Lock()):
                if not any(r["bucket_id"] == bucket_id and not r.get("output_collected")
                           for r in self.db.records("request")):
                    connection = self.connections.pop(bucket_id, None)
                    if connection:
                        connection["task"].cancel()
                        await asyncio.gather(connection["task"], return_exceptions=True)
                        await connection["http"].aclose()

    async def close(self):
        tasks = [*self.tasks.values(), *(c["task"] for c in self.connections.values())]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for connection in self.connections.values():
            await connection["http"].aclose()
