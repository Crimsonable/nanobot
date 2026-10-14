from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import Field

from agent_backend.config import ControlConfig
from agent_backend.journal import Journal
from agent_backend.models import (
    Attachment,
    Envelope,
    Identity,
    OpenCodeEventChunk,
    Result,
    StrictModel,
    Submit,
    digest,
    uid,
)
from agent_backend.wire import Peer
from container_up.channel_service import ChannelService

log = logging.getLogger(__name__)


class Inbound(StrictModel):
    user_id: str
    chat_id: str = Field(min_length=1)
    content: str = Field(max_length=200_000)
    attachments: list[str | dict[str, Any]] = Field(default_factory=list, max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class StarletteSocket:
    def __init__(self, socket: WebSocket):
        self.socket = socket

    async def send(self, data: str):
        await self.socket.send_text(data)

    async def recv(self) -> str:
        packet = await self.socket.receive()
        if packet["type"] == "websocket.disconnect":
            raise ConnectionError("bucket disconnected")
        if packet.get("text") is None:
            raise ValueError("control protocol requires text frames")
        return packet["text"]


class ControlService:
    def __init__(self, config: ControlConfig, channels: ChannelService | None = None):
        self.config = config
        self.db = Journal(config.state_root / "control.sqlite", config.limits.outbox_messages)
        self.channels = channels or ChannelService(config)
        self.peers: dict[str, Peer] = {}
        self.sockets: dict[str, WebSocket] = {}
        self.delivery_task: asyncio.Task | None = None
        self.user_locks: dict[str, asyncio.Lock] = {}
        from container_up.bucket_scheduler import BucketScheduler
        self.pool = BucketScheduler(self)
        from container_up.session_manager import SessionManager
        self.sessions = SessionManager(self)
        self.delivery_ready = asyncio.Event()
        self.completed: dict[str, asyncio.Event] = {}
        self.bucket_connected: dict[str, asyncio.Event] = {}
        self.submission_tasks: set[asyncio.Task] = set()

    def request(self, bucket_id: str, request_id: str) -> dict:
        row = self.db.get("request", request_id)
        if not row or row["bucket_id"] != bucket_id:
            raise PermissionError("request does not belong to this bucket")
        return row

    def require_frontend(self, frontend_id: str) -> None:
        if frontend_id not in self.config.frontends:
            raise HTTPException(404, "frontend is not registered")

    async def normalize(self, values: list) -> list[Attachment]:
        result = []
        for value in values:
            raw = {"url": value} if isinstance(value, str) else value
            reference = str(raw.get("url") or raw.get("path") or "")
            if not reference:
                raise ValueError("attachment requires a URL or file path")
            is_url = bool(urlparse(reference).scheme) and not (len(reference) > 1 and reference[1] == ":")
            filename = raw.get("filename") or Path(urlparse(reference).path.replace("\\", "/")).name or "attachment.bin"
            item = Attachment(url=reference if is_url else "", path="" if is_url else reference,
                filename=filename, requires_auth=bool(raw.get("requires_auth", False)), content_type=raw.get("content_type") or mimetypes.guess_type(filename)[0]
                or "application/octet-stream")
            result.append(item)
        return result

    async def submit(self, frontend_id: str, inbound: Inbound, sync: bool = False) -> dict:
        # A disconnected HTTP caller must not drop an in-memory history or release
        # its lock halfway through a move. Process shutdown remains the accepted
        # failure window for this deliberately memory-only transfer protocol.
        task = asyncio.create_task(self._place(frontend_id, inbound, sync))
        self.submission_tasks.add(task)

        def finished(done):
            self.submission_tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def _place(self, frontend_id: str, inbound: Inbound, sync: bool = False) -> dict:
        self.require_frontend(frontend_id)
        identity = Identity(frontend_id=frontend_id, user_id=inbound.user_id)
        async with self.user_locks.setdefault(identity.identity_id, asyncio.Lock()):
            if inbound.request_id and self.db.get("request", inbound.request_id):
                return await self._submit(frontend_id, inbound, sync)
            async with self.pool.assignment(identity):
                return await self._submit(frontend_id, inbound, sync)

    async def _submit(self, frontend_id: str, inbound: Inbound, sync: bool = False) -> dict:
        frontend = self.config.frontends[frontend_id]
        identity = Identity(frontend_id=frontend_id, user_id=inbound.user_id)
        request_id = inbound.request_id or uid("req_")
        fingerprint = digest({"identity": identity.model_dump(), "input": inbound.model_dump(
            exclude={"request_id"}), "sync": sync})
        existing = self.db.get("request", request_id)
        if existing:
            if existing["hash"] != fingerprint:
                raise HTTPException(409, "request_id already used for different input")
            return existing
        binding = self.db.get("binding", identity.identity_id)
        if binding is None:
            raise RuntimeError("user has no assigned bucket")
        attachments_input = list(inbound.attachments)
        from container_up.attachments import attachment_from_content_url
        auto = attachment_from_content_url(inbound.content, frontend.attachment_url_prefix)
        if auto and not any((a.get("url") if isinstance(a, dict) else a) == auto["url"] for a in attachments_input):
            attachments_input.append(auto)
        attachments = await self.normalize(attachments_input)
        submit = Submit(request_id=request_id, chat_id=inbound.chat_id,
                        identity=identity, content=inbound.content,
                        attachments=attachments, deadline_at=time.time() + self.config.limits.request_seconds)
        if inbound.content.strip().lower() != "/cancel":
            submit.session_id = await self.sessions.prepare(submit, binding["bucket_id"])
            submit.deadline_at = time.time() + self.config.limits.request_seconds
        record = {"request_id": request_id, "hash": fingerprint, "bucket_id": binding["bucket_id"],
                  "chat_id": inbound.chat_id, "metadata": inbound.metadata, "sync": sync,
                  "submit": submit.model_dump(mode="json"), "status": "queued", "delivery": "pending"}
        with self.db.transaction():
            # There may have been an await during normalization.
            if self.db.get("request", request_id):
                raise HTTPException(409, "concurrent duplicate request; query the existing request")
            if self.db.get("binding", identity.identity_id) != binding:
                raise RuntimeError("bucket binding changed during request submission")
            self.db.put("request", request_id, record)
            self.db.enqueue(record["bucket_id"], Envelope(type="request.submit",
                bucket_id=record["bucket_id"], request_id=request_id, payload=submit.model_dump(mode="json")))
        log.info("request queued bucket_id=%s request_id=%s identity_id=%s", record["bucket_id"], request_id, identity.identity_id)
        return record

    async def on_event(self, bucket_id: str, event: Envelope) -> None:
        if event.bucket_id != bucket_id:
            raise PermissionError("bucket event ownership mismatch")
        if event.type == "opencode.event.chunk":
            self.receive_event_chunk(bucket_id, event)
            return
        if event.type == "opencode.event":
            if event.request_id:
                self.request(bucket_id, event.request_id)
            # Persist the whole event for inspection, even when no request is
            # attached or its final result was already delivered to the channel.
            self.db.put("opencode_event", digest([bucket_id, event.event_id]), event.model_dump(mode="json"))
            return
        row = self.request(bucket_id, event.request_id)
        if event.type in {"request.completed", "request.failed", "request.cancelled"}:
            result = Result.model_validate(event.payload)
            if result.request_id != event.request_id or event.type != "request." + result.status:
                raise ValueError("terminal identity mismatch")
            for item in result.attachments:
                if urlparse(item.url).scheme not in {"http", "https"}:
                    raise ValueError("published attachment must contain an object URL")
            if row.get("result"):
                if row["result"] != result.model_dump(mode="json"):
                    raise ValueError("conflicting terminal result")
                return
            self.db.put("request", event.request_id, {**row, "status": result.status,
                        "result": result.model_dump(mode="json")})
            self.pool.touch(bucket_id)
            self.pool.release(Identity.model_validate(row["submit"]["identity"]).identity_id)
            self.delivery_ready.set()
            if completed := self.completed.pop(event.request_id, None):
                completed.set()
        elif event.type in {"request.accepted", "request.started"}:
            if not row.get("result"):
                if event.type == "request.started" and event.payload.get("session_id"):
                    submit = Submit.model_validate(row["submit"])
                    self.db.put("session", submit.session_key, {"identity": submit.identity.model_dump(),
                        "chat_id": submit.chat_id, "session_id": event.payload["session_id"], "bucket_id": bucket_id})
                self.db.put("request", event.request_id, {**row, "status": event.type.split(".")[1]})
        else:
            raise ValueError("unsupported bucket event")

    def receive_event_chunk(self, bucket_id: str, event: Envelope) -> None:
        chunk = OpenCodeEventChunk.model_validate(event.payload)
        if chunk.index >= chunk.count:
            raise ValueError("invalid event chunk index")
        if event.request_id:
            self.request(bucket_id, event.request_id)
        key = digest([bucket_id, chunk.event_id])
        with self.db.transaction():
            if self.db.get("opencode_event", key):
                return
            state = self.db.get("opencode_event_chunks", key) or {
                "count": chunk.count, "request_id": event.request_id, "parts": {}}
            if state["count"] != chunk.count or state["request_id"] != event.request_id:
                raise ValueError("event chunk identity mismatch")
            index = str(chunk.index)
            if index in state["parts"] and state["parts"][index] != chunk.data:
                raise ValueError("conflicting event chunk")
            state["parts"][index] = chunk.data
            if len(state["parts"]) == chunk.count:
                encoded = b"".join(base64.b64decode(state["parts"][str(i)], validate=True)
                                   for i in range(chunk.count))
                original = Envelope.model_validate_json(encoded)
                if (original.type != "opencode.event" or original.event_id != chunk.event_id
                        or original.bucket_id != bucket_id or original.request_id != event.request_id):
                    raise ValueError("reassembled event identity mismatch")
                self.db.put("opencode_event", key, original.model_dump(mode="json"))
                self.db.delete("opencode_event_chunks", key)
            else:
                self.db.put("opencode_event_chunks", key, state)

    async def dispatch_channel(self, frontend_id: str, payload: dict) -> None:
        event = payload.get("event") or {}
        try:
            await self.submit(frontend_id, Inbound(user_id=event["user_id"],
                chat_id=event["chat_id"], content=event.get("content") or "",
                attachments=event.get("attachments") or [], metadata=event.get("metadata") or {}))
        except Exception:
            log.exception("subscribe dispatch failed frontend_id=%s chat_id=%s",
                          frontend_id, event.get("chat_id"))
            # The HTTP subscriber must see the failure rather than acknowledging lost input.
            raise

    async def deliveries(self) -> None:
        # A crash after sending but before saving success is ambiguous. Never blindly resend.
        for row in self.db.records("request"):
            if row["delivery"] == "sending":
                self.db.put("request", row["request_id"], {**row, "delivery": "unknown"})
        while True:
            self.delivery_ready.clear()
            for row in self.db.records("request"):
                if row["sync"] or not row.get("result") or row["delivery"] != "pending":
                    continue
                result = Result.model_validate(row["result"])
                if result.status == "cancelled" or not (result.content or result.attachments):
                    self.db.put("request", row["request_id"], {**row, "delivery": "skipped"})
                    continue
                self.db.put("request", row["request_id"], {**row, "delivery": "sending"})
                try:
                    await self.channels.deliver(row, result)
                    status = "sent"
                except Exception:
                    log.exception("channel delivery uncertain request_id=%s", row["request_id"])
                    status = "unknown"
                latest = self.db.get("request", row["request_id"])
                self.db.put("request", row["request_id"], {**latest, "delivery": status})
            await self.delivery_ready.wait()


def create_app(config: ControlConfig, channels: ChannelService | None = None) -> FastAPI:
    service = ControlService(config, channels)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.channels.start(service.dispatch_channel)
        service.delivery_task = asyncio.create_task(service.deliveries())
        service.pool.task = asyncio.create_task(service.pool.run())
        try:
            yield
        finally:
            await service.pool.stop()
            for task in service.submission_tasks:
                task.cancel()
            await asyncio.gather(*service.submission_tasks, return_exceptions=True)
            service.delivery_task.cancel()
            await asyncio.gather(service.delivery_task, return_exceptions=True)
            for socket in list(service.sockets.values()):
                await socket.close()
            await service.channels.stop()
            service.db.close()

    app = FastAPI(title="Agent channel control", lifespan=lifespan)
    app.state.service = service

    @app.exception_handler(ValueError)
    async def invalid_input(request: Request, exc: ValueError):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(exc)[:500]}, status_code=400)

    @app.exception_handler(PermissionError)
    async def forbidden_input(request: Request, exc: PermissionError):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "operation is not permitted"}, status_code=403)

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        return {"status": "ready", "connected_buckets": sorted(service.peers)}

    @app.websocket("/internal/buckets/ws")
    async def bucket_socket(socket: WebSocket):
        bucket_id = socket.headers.get("x-bucket-id", "")
        bucket = service.db.get("bucket", bucket_id)
        registered = bool(bucket and bucket["status"] in {"creating", "running"})
        if not registered:
            await socket.close(code=1008)
            return
        await socket.accept()
        old = service.sockets.get(bucket_id)
        if old:
            await old.close(code=1012)
        peer: Peer

        async def on_event(event: Envelope):
            if service.peers.get(bucket_id) is not peer:
                raise PermissionError("superseded bucket connection")
            await service.on_event(bucket_id, event)

        peer = Peer(StarletteSocket(socket), service.db, bucket_id, on_event)
        service.peers[bucket_id], service.sockets[bucket_id] = peer, socket
        service.bucket_connected.setdefault(bucket_id, asyncio.Event()).set()
        try:
            await peer.run()
        except (WebSocketDisconnect, ConnectionError, RuntimeError, ValueError, TimeoutError):
            log.info("bucket connection ended bucket_id=%s", bucket_id)
        finally:
            if service.peers.get(bucket_id) is peer:
                service.bucket_connected[bucket_id].clear()
                service.peers.pop(bucket_id, None)
                service.sockets.pop(bucket_id, None)

    async def response(row: dict) -> dict:
        identity = row["submit"]["identity"]
        result = dict(row.get("result") or {})
        return {"request_id": row["request_id"], "status": row["status"], **identity,
                "chat_id": row["chat_id"], "bucket_id": row["bucket_id"],
                "identity_id": Identity.model_validate(identity).identity_id,
                "metadata": row["metadata"],
                "delivery_state": row["delivery"], **result}

    @app.post("/inbound/{frontend_id}")
    async def inbound(frontend_id: str, body: Inbound, request: Request):
        service.require_frontend(frontend_id)
        return await response(await service.submit(frontend_id, body))

    @app.post("/inbound-sync/{frontend_id}")
    async def inbound_sync(frontend_id: str, body: Inbound, request: Request):
        service.require_frontend(frontend_id)
        row = await service.submit(frontend_id, body, sync=True)
        completed = service.completed.setdefault(row["request_id"], asyncio.Event())
        try:
            row = service.db.get("request", row["request_id"])
            if not row.get("result"):
                await asyncio.wait_for(completed.wait(), config.limits.request_seconds)
            row = service.db.get("request", row["request_id"])
        except TimeoutError:
            raise HTTPException(504, {"request_id": row["request_id"], "error": "reply timeout"})
        finally:
            if completed.is_set() or row.get("result"):
                service.completed.pop(row["request_id"], None)
        return await response(row)

    @app.get("/requests/{frontend_id}/{request_id}")
    async def query(frontend_id: str, request_id: str, request: Request):
        service.require_frontend(frontend_id)
        row = service.db.get("request", request_id)
        if not row or row["submit"]["identity"]["frontend_id"] != frontend_id:
            raise HTTPException(404, "request not found")
        return await response(row)

    @app.post("/requests/{frontend_id}/{request_id}/cancel")
    async def cancel(frontend_id: str, request_id: str, request: Request):
        await query(frontend_id, request_id, request)
        row = service.db.get("request", request_id)
        if not row.get("result"):
            service.db.enqueue(row["bucket_id"], Envelope(type="request.cancel", bucket_id=row["bucket_id"],
                                                         request_id=request_id))
        return await response(row)

    @app.post("/subscribe/{frontend_id}")
    async def subscribe(frontend_id: str, request: Request):
        # QXT verifies its signed callback form inside process_subscribe_form.
        from types import SimpleNamespace
        parser = service.channels.parsers.get(frontend_id)
        if parser is None or not parser.supports_subscribe():
            raise HTTPException(404, "subscription not available")
        response_value, payload = parser.process_subscribe_form(SimpleNamespace(**await request.json()))
        if payload is not None:
            await service.dispatch_channel(frontend_id, payload)
        return response_value

    return app
