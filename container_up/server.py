from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from agent_backend.config import ControlConfig
from agent_backend.ingress import Inbound
from agent_backend.journal import Journal
from agent_backend.models import (
    Attachment,
    Envelope,
    ExecutionStatus,
    Identity,
    Submit,
    digest,
    uid,
)
from agent_backend.sockets import StarletteSocket
from agent_backend.wire import Peer

log = logging.getLogger(__name__)


class ControlService:
    def __init__(self, config: ControlConfig):
        self.config = config
        self.db = Journal(config.state_root / "control.sqlite", config.limits.outbox_messages)
        self.peers: dict[str, Peer] = {}
        self.sockets: dict[str, WebSocket] = {}
        self.user_locks: dict[str, asyncio.Lock] = {}
        from container_up.bucket_scheduler import BucketScheduler
        self.pool = BucketScheduler(self)
        from container_up.session_manager import SessionManager
        self.sessions = SessionManager(self)
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

    async def submit(self, frontend_id: str, inbound: Inbound) -> dict:
        # A disconnected HTTP caller must not drop an in-memory history or release
        # its lock halfway through a move. Process shutdown remains the accepted
        # failure window for this deliberately memory-only transfer protocol.
        task = asyncio.create_task(self._place(frontend_id, inbound))
        self.submission_tasks.add(task)

        def finished(done):
            self.submission_tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def _place(self, frontend_id: str, inbound: Inbound) -> dict:
        self.require_frontend(frontend_id)
        identity = Identity(frontend_id=frontend_id, user_id=inbound.user_id)
        async with self.user_locks.setdefault(identity.identity_id, asyncio.Lock()):
            if inbound.request_id and self.db.get("request", inbound.request_id):
                return await self._submit(frontend_id, inbound)
            async with self.pool.assignment(identity):
                return await self._submit(frontend_id, inbound)

    async def _submit(self, frontend_id: str, inbound: Inbound) -> dict:
        identity = Identity(frontend_id=frontend_id, user_id=inbound.user_id)
        request_id = inbound.request_id or uid("req_")
        fingerprint = digest({"identity": identity.model_dump(), "input": inbound.model_dump(
            exclude={"request_id"})})
        existing = self.db.get("request", request_id)
        if existing:
            if existing["hash"] != fingerprint:
                raise HTTPException(409, "request_id already used for different input")
            return existing
        binding = self.db.get("binding", identity.identity_id)
        if binding is None:
            raise RuntimeError("user has no assigned bucket")
        attachments_input = list(inbound.attachments)
        attachments = await self.normalize(attachments_input)
        submit = Submit(request_id=request_id, chat_id=inbound.chat_id,
                        identity=identity, content=inbound.content,
                        attachments=attachments, deadline_at=time.time() + self.config.limits.request_seconds,
                        stream=inbound.stream)
        if inbound.content.strip().lower() != "/cancel":
            submit.session_id = await self.sessions.prepare(submit, binding["bucket_id"])
            submit.deadline_at = time.time() + self.config.limits.request_seconds
        record = {"request_id": request_id, "hash": fingerprint, "bucket_id": binding["bucket_id"],
                  "chat_id": inbound.chat_id,
                  "submit": submit.model_dump(mode="json"), "status": "queued"}
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
        if event.type == "bucket.status":
            bucket = self.db.get("bucket", bucket_id)
            if not bucket or not isinstance(event.payload.get("accepting"), bool):
                raise ValueError("invalid bucket status")
            self.db.put("bucket", bucket_id, {**bucket, "accepting": event.payload["accepting"]})
            return
        row = self.request(bucket_id, event.request_id)
        if event.type in {"request.completed", "request.failed", "request.cancelled"}:
            result = ExecutionStatus.model_validate(event.payload)
            if result.request_id != event.request_id or event.type != "request." + result.status:
                raise ValueError("terminal identity mismatch")
            if row.get("result"):
                if row["result"] != result.model_dump(mode="json"):
                    raise ValueError("conflicting terminal result")
                return
            self.db.put("request", event.request_id, {**row, "status": result.status,
                        "result": result.model_dump(mode="json")})
            self.pool.touch(bucket_id)
            self.pool.release(Identity.model_validate(row["submit"]["identity"]).identity_id)
        elif event.type in {"request.accepted", "request.started"}:
            if not row.get("result"):
                if event.type == "request.started" and event.payload.get("session_id"):
                    submit = Submit.model_validate(row["submit"])
                    self.db.put("session", submit.session_key, {"identity": submit.identity.model_dump(),
                        "chat_id": submit.chat_id, "session_id": event.payload["session_id"], "bucket_id": bucket_id})
                self.db.put("request", event.request_id, {**row, "status": event.type.split(".")[1]})
        else:
            raise ValueError("unsupported bucket event")



def create_app(config: ControlConfig) -> FastAPI:
    service = ControlService(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service.pool.task = asyncio.create_task(service.pool.run())
        try:
            yield
        finally:
            await service.pool.stop()
            for task in service.submission_tasks:
                task.cancel()
            await asyncio.gather(*service.submission_tasks, return_exceptions=True)
            for socket in list(service.sockets.values()):
                await socket.close()
            service.db.close()

    app = FastAPI(title="Agent bucket control", lifespan=lifespan)
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
        registered = bool(bucket and bucket["status"] in {"creating", "running", "draining"})
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

    @app.post("/internal/submit/{frontend_id}")
    async def submit(frontend_id: str, body: Inbound):
        return await service.submit(frontend_id, body)

    @app.get("/internal/requests/{frontend_id}/{request_id}")
    async def query(frontend_id: str, request_id: str):
        service.require_frontend(frontend_id)
        row = service.db.get("request", request_id)
        if not row or row["submit"]["identity"]["frontend_id"] != frontend_id:
            raise HTTPException(404, "request not found")
        return row

    @app.post("/internal/requests/{frontend_id}/{request_id}/cancel")
    async def cancel(frontend_id: str, request_id: str):
        row = await query(frontend_id, request_id)
        if not row.get("result"):
            service.db.enqueue(row["bucket_id"], Envelope(type="request.cancel", bucket_id=row["bucket_id"],
                                                         request_id=request_id))
        return row

    @app.get("/internal/buckets/{bucket_id}")
    async def bucket(bucket_id: str):
        row = service.db.get("bucket", bucket_id)
        if not row or row["status"] not in {"creating", "running", "draining"}:
            raise HTTPException(404, "bucket not registered")
        return row

    return app
