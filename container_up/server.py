from __future__ import annotations

import asyncio
import logging
import mimetypes
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import Field

from agent_backend.config import ControlConfig, secret
from agent_backend.journal import Journal
from agent_backend.models import (
    Attachment,
    Envelope,
    Identity,
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
    chat_id: str = "default"
    content: str = Field(max_length=200_000)
    attachments: list[str | dict[str, Any]] = Field(default_factory=list, max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class StarletteSocket:
    def __init__(self, socket: WebSocket):
        self.socket = socket

    async def send(self, data: str | bytes):
        if isinstance(data, bytes):
            await self.socket.send_bytes(data)
        else:
            await self.socket.send_text(data)

    async def recv(self) -> str | bytes:
        packet = await self.socket.receive()
        if packet["type"] == "websocket.disconnect":
            raise ConnectionError("bucket disconnected")
        return packet.get("bytes") if packet.get("bytes") is not None else packet["text"]


class ControlService:
    def __init__(self, config: ControlConfig, channels: ChannelService | None = None):
        self.config = config
        self.db = Journal(config.state_root / "control.sqlite", config.limits.outbox_messages)
        self.channels = channels or ChannelService(config)
        self.peers: dict[str, Peer] = {}
        self.sockets: dict[str, WebSocket] = {}
        self.delivery_task: asyncio.Task | None = None
        self.user_locks: dict[str, asyncio.Lock] = {}
        self.pool = None
        if config.bucket_pool:
            from container_up.bucket_scheduler import BucketScheduler
            self.pool = BucketScheduler(self)

    def request(self, bucket_id: str, request_id: str) -> dict:
        row = self.db.get("request", request_id)
        if not row or row["bucket_id"] != bucket_id:
            raise PermissionError("request does not belong to this bucket")
        return row

    def authenticate(self, frontend_id: str, authorization: str | None) -> None:
        frontend = self.config.frontends.get(frontend_id)
        if not frontend or not secrets.compare_digest(
                authorization or "", "Bearer " + secret(frontend.api_key_file)):
            raise HTTPException(401, "invalid frontend credential")

    async def normalize(self, identity: Identity, values: list, trusted_channel: bool) -> list[Attachment]:
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

    async def submit(self, frontend_id: str, inbound: Inbound, sync: bool = False,
                     trusted_channel: bool = False) -> dict:
        if not self.pool:
            return await self._submit(frontend_id, inbound, sync, trusted_channel)
        identity = Identity(frontend_id=frontend_id, user_id=inbound.user_id)
        async with self.user_locks.setdefault(identity.key, asyncio.Lock()):
            if inbound.request_id and self.db.get("request", inbound.request_id):
                return await self._submit(frontend_id, inbound, sync, trusted_channel)
            async with self.pool.assignment(identity):
                return await self._submit(frontend_id, inbound, sync, trusted_channel)

    async def _submit(self, frontend_id: str, inbound: Inbound, sync: bool = False,
                      trusted_channel: bool = False) -> dict:
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
        binding = self.db.get("binding", identity.key)
        if binding is None:
            available = [key for key, peer in self.peers.items() if not peer.closed]
            if not available:
                raise HTTPException(503, "no connected bucket")
            counts = {key: sum(b["bucket_id"] == key for b in self.db.records("binding"))
                      for key in available}
            bucket_id = min(available, key=lambda k: (counts[k], k))
            binding = {"bucket_id": bucket_id, "user_key": identity.key,
                       "identity": identity.model_dump()}
        conversation_id = digest([identity.key, inbound.chat_id])
        attachments_input = list(inbound.attachments)
        from container_up.attachments import attachment_from_content_url
        auto = attachment_from_content_url(inbound.content, frontend.attachment_url_prefix)
        if auto and not any((a.get("url") if isinstance(a, dict) else a) == auto["url"] for a in attachments_input):
            attachments_input.append(auto)
        attachments = await self.normalize(identity, attachments_input, trusted_channel)
        submit = Submit(request_id=request_id, conversation_id=conversation_id,
                        identity=identity, profile_id=frontend.profile_id, content=inbound.content,
                        attachments=attachments, deadline_at=time.time() + self.config.limits.request_seconds)
        if self.pool:
            submit.session_epoch = binding["generation"]
            history = [r for r in self.db.records("request") if r.get("result")
                       and r["submit"]["conversation_id"] == conversation_id
                       and r["submit"]["identity"] == identity.model_dump()]
            submit.previous_turns = [{"user": r["submit"]["content"],
                                      "input_attachments": r["submit"].get("attachments", []),
                                      "assistant": r["result"]["content"],
                                      "attachments": r["result"].get("attachments", [])}
                                     for r in history[-32:]]
            # Keep the metadata envelope bounded; full results remain queryable in CU's database.
            import json
            while submit.previous_turns and len(json.dumps(submit.previous_turns).encode()) > 128_000:
                submit.previous_turns.pop(0)
        record = {"request_id": request_id, "hash": fingerprint, "bucket_id": binding["bucket_id"],
                  "chat_id": inbound.chat_id, "metadata": inbound.metadata, "sync": sync,
                  "submit": submit.model_dump(mode="json"), "status": "queued", "delivery": "pending"}
        with self.db.transaction():
            # There may have been an await during normalization.
            if self.db.get("request", request_id):
                raise HTTPException(409, "concurrent duplicate request; query the existing request")
            current = self.db.get("binding", identity.key)
            if current:
                record["bucket_id"] = current["bucket_id"]
            else:
                self.db.put("binding", identity.key, binding)
            self.db.put("request", request_id, record)
            self.db.enqueue(record["bucket_id"], Envelope(type="request.submit",
                bucket_id=record["bucket_id"], request_id=request_id, payload=submit.model_dump(mode="json")))
        log.info("request queued bucket_id=%s request_id=%s user_key=%s", record["bucket_id"], request_id, identity.key)
        return record

    async def on_event(self, bucket_id: str, event: Envelope) -> None:
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
            if self.pool:
                self.pool.touch(bucket_id)
        elif event.type in {"request.accepted", "request.started", "request.recovering"}:
            if not row.get("result"):
                self.db.put("request", event.request_id, {**row, "status": event.type.split(".")[1]})
        elif event.type == "response.delta":
            if not row.get("result") and event.payload.get("revision", 0) > row.get("progress", {}).get("revision", 0):
                self.db.put("request", event.request_id, {**row, "progress": event.payload})
        elif event.type != "tool.progress":
            raise ValueError("unsupported bucket event")

    async def rpc(self, bucket_id: str, method: str, args: dict, data: bytes | None):
        self.request(bucket_id, args.get("request_id", ""))
        raise ValueError("file transfer RPC is not supported; pass attachment references")

    async def dispatch_channel(self, frontend_id: str, payload: dict) -> None:
        event = payload["event"]
        await self.submit(frontend_id, Inbound(user_id=event["usr_id"],
            chat_id=event.get("chat_id") or "default", content=event.get("content") or "",
            attachments=event.get("attachments") or [], metadata=event.get("metadata") or {}),
            trusted_channel=True)

    async def deliveries(self) -> None:
        # A crash after sending but before saving success is ambiguous. Never blindly resend.
        for row in self.db.records("request"):
            if row["delivery"] == "sending":
                self.db.put("request", row["request_id"], {**row, "delivery": "unknown"})
        while True:
            for row in self.db.records("request"):
                if row["sync"] or not row.get("result") or row["delivery"] != "pending":
                    continue
                result = Result.model_validate(row["result"])
                if result.status == "failed" and not result.content:
                    result.content = f"任务执行未完成，请使用请求编号 {result.request_id} 查询详情。"
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
            await asyncio.sleep(0.2)


def create_app(config: ControlConfig, channels: ChannelService | None = None) -> FastAPI:
    service = ControlService(config, channels)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.channels.start(service.dispatch_channel)
        service.delivery_task = asyncio.create_task(service.deliveries())
        if service.pool:
            service.pool.task = asyncio.create_task(service.pool.run())
        try:
            yield
        finally:
            if service.pool:
                await service.pool.stop()
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
        registered = bucket_id in config.bucket_ids
        if service.pool:
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

        async def rpc(method: str, args: dict, data: bytes | None):
            if service.peers.get(bucket_id) is not peer:
                raise PermissionError("superseded bucket connection")
            return await service.rpc(bucket_id, method, args, data)

        peer = Peer(StarletteSocket(socket), service.db, bucket_id, on_event, rpc)
        service.peers[bucket_id], service.sockets[bucket_id] = peer, socket
        try:
            await peer.run()
        except (WebSocketDisconnect, ConnectionError, RuntimeError, ValueError, TimeoutError):
            log.info("bucket connection ended bucket_id=%s", bucket_id)
        finally:
            if service.peers.get(bucket_id) is peer:
                service.peers.pop(bucket_id, None)
                service.sockets.pop(bucket_id, None)

    async def response(row: dict) -> dict:
        identity = row["submit"]["identity"]
        result = dict(row.get("result") or {})
        return {"request_id": row["request_id"], "status": row["status"], **identity,
                "chat_id": row["chat_id"], "bucket_id": row["bucket_id"],
                "instance_id": Identity.model_validate(identity).key,
                "conversation_id": row["submit"]["conversation_id"],
                "metadata": row["metadata"],
                "progress": row.get("progress"),
                "delivery_state": row["delivery"], **result}

    @app.post("/inbound/{frontend_id}")
    async def inbound(frontend_id: str, body: Inbound, request: Request):
        service.authenticate(frontend_id, request.headers.get("authorization"))
        return await response(await service.submit(frontend_id, body))

    @app.post("/inbound-sync/{frontend_id}")
    async def inbound_sync(frontend_id: str, body: Inbound, request: Request):
        service.authenticate(frontend_id, request.headers.get("authorization"))
        row = await service.submit(frontend_id, body, sync=True)
        until = time.monotonic() + config.limits.request_seconds
        while not row.get("result"):
            if time.monotonic() >= until:
                raise HTTPException(504, {"request_id": row["request_id"], "error": "reply timeout; query request"})
            await asyncio.sleep(0.05)
            row = service.db.get("request", row["request_id"])
        return await response(row)

    @app.get("/requests/{frontend_id}/{request_id}")
    async def query(frontend_id: str, request_id: str, request: Request):
        service.authenticate(frontend_id, request.headers.get("authorization"))
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

    @app.post("/api/message")
    async def message_alias(request: Request):
        body = await request.json()
        frontend_id = body.pop("frontend_id", "")
        body["user_id"] = body.pop("usr_id")
        return await inbound(frontend_id, Inbound.model_validate(body), request)

    @app.post("/api/cancel")
    async def cancel_alias(request: Request):
        body = await request.json()
        frontend_id = body["frontend_id"]
        service.authenticate(frontend_id, request.headers.get("authorization"))
        matches = [row for row in service.db.records("request") if not row.get("result")
            and row["submit"]["identity"] == {"frontend_id": frontend_id, "user_id": body["usr_id"]}
            and row["chat_id"] == body.get("chat_id", "default")]
        for row in matches:
            service.db.enqueue(row["bucket_id"], Envelope(type="request.cancel",
                bucket_id=row["bucket_id"], request_id=row["request_id"]))
        return {"status": "accepted", "request_ids": [r["request_id"] for r in matches]}

    @app.post("/internal/attachments/upload")
    async def upload_attachment(request: Request):
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(400, "send a JSON attachment URL or path; file uploads are handled by publish_attachment")
        body = await request.json()
        service.authenticate(body["frontend_id"], request.headers.get("authorization"))
        reference = {"url": body.get("attachment_url") or body.get("url"), "path": body.get("path"),
                     "filename": body.get("filename"), "content_type": body.get("content_type")}
        items = await service.normalize(Identity(frontend_id=body["frontend_id"],
            user_id=str(body.get("user_id") or "reference")), [reference], False)
        return items[0].model_dump(mode="json")

    return app
