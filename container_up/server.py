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
from agent_backend.transfers import Transfers, file_hash, send_file
from agent_backend.wire import Peer
from container_up.artifact_service import ArtifactService
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
        self.artifacts = ArtifactService(config, self.db)
        self.transfers = Transfers(config.state_root / "transfers", self.db,
                                   config.limits.file_bytes, config.limits.spool_bytes)
        self.channels = channels or ChannelService(config)
        self.peers: dict[str, Peer] = {}
        self.sockets: dict[str, WebSocket] = {}
        self.delivery_task: asyncio.Task | None = None

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
            url = str(raw.get("url") or raw.get("path") or "")
            filename = raw.get("filename") or Path(urlparse(url).path).name or "attachment.bin"
            item = Attachment(url=url, filename=filename,
                content_type=raw.get("content_type") or mimetypes.guess_type(filename)[0]
                or "application/octet-stream")
            if urlparse(url).scheme not in {"http", "https"}:
                cache = self.config.state_root.resolve() / "channel-cache" / digest(
                    [identity.frontend_id, identity.user_id])
                path = Path(url).resolve(strict=True)
                if not trusted_channel or not path.is_relative_to(cache) or not path.is_file():
                    raise ValueError("untrusted local attachment")
                size = path.stat().st_size
                if size > self.config.limits.file_bytes:
                    raise ValueError("attachment exceeds limit")
                item = item.model_copy(update={"size": size, "sha256": file_hash(path)})
                item = await self.artifacts.upload(identity, item.attachment_id, path, item)
            result.append(item)
        return result

    async def submit(self, frontend_id: str, inbound: Inbound, sync: bool = False,
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
            identity = Identity.model_validate(row["submit"]["identity"])
            for item in result.attachments:
                stored = self.db.get("artifact", item.attachment_id)
                if not stored or stored["owner"] != identity.key:
                    raise PermissionError("unregistered output attachment")
                canonical = Attachment.model_validate(stored["attachment"])
                if item.sha256 != canonical.sha256 or item.object_key != canonical.object_key:
                    raise PermissionError("output attachment mismatch")
            if row.get("result"):
                if row["result"] != result.model_dump(mode="json"):
                    raise ValueError("conflicting terminal result")
                return
            self.db.put("request", event.request_id, {**row, "status": result.status,
                        "result": result.model_dump(mode="json")})
        elif event.type in {"request.accepted", "request.started", "request.recovering"}:
            if not row.get("result"):
                self.db.put("request", event.request_id, {**row, "status": event.type.split(".")[1]})
        elif event.type == "response.delta":
            if not row.get("result") and event.payload.get("revision", 0) > row.get("progress", {}).get("revision", 0):
                self.db.put("request", event.request_id, {**row, "progress": event.payload})
        elif event.type != "tool.progress":
            raise ValueError("unsupported bucket event")

    async def rpc(self, bucket_id: str, method: str, args: dict, data: bytes | None) -> dict:
        if method == "input.fetch":
            row = self.request(bucket_id, args["request_id"])
            submit = Submit.model_validate(row["submit"])
            attachment = next((a for a in submit.attachments if a.attachment_id == args["attachment_id"]), None)
            if attachment is None:
                raise PermissionError("unknown inbound attachment")
            attachment, path = await self.artifacts.download(submit.identity, attachment)
            result = await send_file(self.peers[bucket_id], path, attachment, submit.request_id,
                                      attachment.attachment_id, prefix="input")
            path.unlink(missing_ok=True)
            return result
        if method == "attachment.begin":
            row = self.request(bucket_id, args["request_id"])
            if row.get("result"):
                raise ValueError("request already finished")
            if args["transfer_id"] != args["attachment"]["attachment_id"]:
                raise ValueError("artifact ID must equal transfer ID")
            existing = self.db.get("artifact", args["transfer_id"])
            if existing:
                transfer = self.transfers.record(bucket_id, args["transfer_id"])
                if transfer["request_id"] != args["request_id"] or transfer["attachment"] != args["attachment"]:
                    raise ValueError("transfer mismatch")
                return {"result": existing["attachment"]}
            return self.transfers.begin(bucket_id, args)
        if method == "attachment.chunk":
            transfer = self.transfers.record(bucket_id, args["transfer_id"])
            if self.request(bucket_id, transfer["request_id"]).get("result"):
                raise ValueError("request already finished")
            return self.transfers.chunk(bucket_id, args, data)
        if method == "attachment.commit":
            transfer_id = args["transfer_id"]
            lock = self.transfers.locks.setdefault(transfer_id, asyncio.Lock())
            async with lock:
                transfer = self.transfers.record(bucket_id, transfer_id)
                row = self.request(bucket_id, transfer["request_id"])
                existing = self.db.get("artifact", transfer_id)
                if existing:
                    return existing["attachment"]
                transfer, path = self.transfers.verify(bucket_id, transfer_id)
                result = await self.artifacts.upload(Identity.model_validate(row["submit"]["identity"]),
                    transfer_id, path, Attachment.model_validate(transfer["attachment"]))
                self.transfers.release(bucket_id, transfer_id)
                return result.model_dump(mode="json")
        raise ValueError("unsupported runtime RPC")

    async def dispatch_channel(self, frontend_id: str, payload: dict) -> None:
        parser = self.channels.parsers[frontend_id]
        # QXT URLs go through ArtifactService's address/size guards during input.fetch.
        # Its legacy preparation helper downloads arbitrary URLs before those guards.
        if self.config.frontends[frontend_id].provider != "qxt" and hasattr(parser, "prepare_inbound_event"):
            payload = await parser.prepare_inbound_event(payload)
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
                    for item in result.attachments:
                        if item.object_key:
                            item.url = await asyncio.to_thread(self.artifacts.url, item.object_key)
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
        try:
            yield
        finally:
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
        credential = config.bucket_credentials.get(bucket_id)
        if not credential or not secrets.compare_digest(socket.headers.get("authorization", ""),
                                                        "Bearer " + secret(credential)):
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
        if result.get("attachments"):
            attachments = []
            for item in result["attachments"]:
                item = dict(item)
                if item.get("object_key"):
                    item["url"] = await asyncio.to_thread(service.artifacts.url, item["object_key"])
                attachments.append(item)
            result["attachments"] = attachments
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
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            body = await request.json()
            service.authenticate(body["frontend_id"], request.headers.get("authorization"))
            url = body.get("attachment_url") or body.get("url") or ""
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise HTTPException(400, "attachment_url must be HTTP(S)")
            name = Path(parsed.path).name or "attachment.bin"
            return Attachment(url=url, filename=name, content_type=mimetypes.guess_type(name)[0]
                              or "application/octet-stream").model_dump(mode="json")
        if not content_type.startswith("multipart/form-data"):
            raise HTTPException(400, "expected multipart file or JSON attachment_url")
        form = await request.form(max_files=1, max_fields=4)
        frontend_id = str(form.get("frontend_id") or "")
        service.authenticate(frontend_id, request.headers.get("authorization"))
        identity = Identity(frontend_id=frontend_id, user_id=str(form.get("user_id") or ""))
        upload = form.get("file")
        if not hasattr(upload, "read"):
            raise HTTPException(400, "file is required")
        attachment_id = uid("att_")
        path = service.artifacts.root / attachment_id
        size = 0
        try:
            with path.open("wb") as target:
                while chunk := await upload.read(256 * 1024):
                    size += len(chunk)
                    if size > config.limits.file_bytes:
                        raise HTTPException(413, "file too large")
                    target.write(chunk)
            attachment = Attachment(attachment_id=attachment_id, filename=upload.filename or "attachment.bin",
                content_type=upload.content_type or "application/octet-stream", size=size, sha256=file_hash(path))
            result = await service.artifacts.upload(identity, attachment_id, path, attachment)
            return result.model_dump(mode="json")
        finally:
            path.unlink(missing_ok=True)
            await upload.close()

    return app
