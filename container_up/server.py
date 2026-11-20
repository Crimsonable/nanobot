from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from functools import partial

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from agent_backend.config import ControlConfig
from agent_backend.ingress import Inbound
from agent_backend.journal import Journal
from agent_backend.models import Envelope, Identity, Submit, digest, uid
from agent_backend.sockets import StarletteSocket
from agent_backend.wire import Peer
from container_up.bucket_manager import BucketMissing
from container_up.bucket_scheduler import BucketBusy, BucketScheduler
from container_up.session_manager import SessionManager, SourceCall

log = logging.getLogger(__name__)


class ControlService:
    def __init__(self, config: ControlConfig):
        self.config = config
        self.db = Journal(config.state_root / "control.sqlite", config.limits.outbox_messages)
        self.user_locks: dict[str, asyncio.Lock] = {}
        self.pool = BucketScheduler(config, self.db, self.interrupt_bucket_requests)
        self.sessions = SessionManager(config, self.db, self.open_session_source)
        self.pending: set[asyncio.Task] = set()

    def _shield(self, work):
        """Finish accepted submissions and resets even if the HTTP caller disconnects."""
        task = asyncio.create_task(work)
        self.pending.add(task)

        def finished(done):
            self.pending.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return asyncio.shield(task)

    async def reset_frontend_opencode(self, frontend_id: str) -> dict:
        self.require_frontend(frontend_id)
        targets, skipped = self.pool.reset_targets(frontend_id)

        async def reset(bucket_id):
            try:
                await self.pool.reset(bucket_id, sessions_idle=self.sessions.can_reset(bucket_id))
                return {"bucket_id": bucket_id, "status": "ready"}
            except KeyError:
                code, detail = 404, "bucket not registered"
            except BucketBusy as exc:
                code, detail = 409, str(exc)
            except ConnectionError as exc:
                code, detail = 503, str(exc)
            except RuntimeError as exc:
                code, detail = 502, str(exc)
            return {"bucket_id": bucket_id, "status": "failed", "status_code": code, "detail": detail}

        results = await asyncio.gather(*(self._shield(reset(bucket_id)) for bucket_id in targets))
        status = "ready" if targets else "no_running_buckets"
        if any(result["status"] == "failed" for result in results):
            status = "partial_failure"
        return {"frontend_id": frontend_id, "status": status, "buckets": results,
                "skipped_buckets": skipped}

    @contextmanager
    def open_session_source(self, bucket_id: str) -> Iterator[SourceCall]:
        with self.pool.hold(bucket_id):
            yield partial(self.pool.call_if_running, bucket_id)

    def interrupt_bucket_requests(self, bucket_id: str) -> None:
        with self.db.transaction():
            for identity_id in self.sessions.interrupt_requests(bucket_id):
                self.pool.touch(bucket_id)
                self.pool.release(identity_id)

    def require_frontend(self, frontend_id: str) -> None:
        if frontend_id not in self.config.frontends:
            raise HTTPException(404, "frontend is not registered")

    async def submit(self, frontend_id: str, inbound: Inbound) -> dict:
        return await self._shield(self._place(frontend_id, inbound))

    def _existing_request(self, request_id: str, fingerprint: str) -> dict | None:
        row = self.db.get("request", request_id)
        if row and row["hash"] != fingerprint:
            raise HTTPException(409, "request_id already used for different input")
        return row

    async def _place(self, frontend_id: str, inbound: Inbound) -> dict:
        self.require_frontend(frontend_id)
        identity = Identity(frontend_id=frontend_id, user_id=inbound.user_id)
        request_id = inbound.request_id or uid("req_")
        fingerprint = digest({"identity": identity.model_dump(), "input": inbound.model_dump(
            exclude={"request_id"})})
        async with self.user_locks.setdefault(identity.identity_id, asyncio.Lock()):
            if existing := self._existing_request(request_id, fingerprint):
                return existing
            submit = Submit(request_id=request_id, chat_id=inbound.chat_id, identity=identity,
                            content=inbound.content, attachments=inbound.attachment_references(),
                            metadata=inbound.metadata, stream=inbound.stream,
                            deadline_at=time.time() + self.config.limits.request_seconds)
            while True:
                try:
                    async with self.pool.assignment(identity) as binding:
                        # Another identity may have claimed the ID during bucket startup.
                        if existing := self._existing_request(request_id, fingerprint):
                            return existing
                        bucket_id = binding["bucket_id"]
                        submit.session_id = await self.sessions.prepare(submit, bucket_id)
                        submit.deadline_at = time.time() + self.config.limits.request_seconds
                        await self.pool.ready(bucket_id)
                        return self._enqueue(submit, binding, fingerprint)
                except BucketMissing:
                    continue

    def _enqueue(self, submit: Submit, binding: dict, fingerprint: str) -> dict:
        request_id = submit.request_id
        record = {"request_id": request_id, "hash": fingerprint, "bucket_id": binding["bucket_id"],
                  "chat_id": submit.chat_id,
                  "submit": submit.model_dump(mode="json"), "status": "queued"}
        with self.db.transaction():
            if self.db.get("request", request_id):
                raise HTTPException(409, "concurrent duplicate request; query the existing request")
            if self.db.get("binding", submit.identity.identity_id) != binding:
                raise RuntimeError("bucket binding changed during request submission")
            self.db.put("request", request_id, record)
            self.db.enqueue(record["bucket_id"], Envelope(type="request.submit",
                bucket_id=record["bucket_id"], request_id=request_id, payload=record["submit"]))
        log.info("request queued bucket_id=%s request_id=%s identity_id=%s", record["bucket_id"], request_id, submit.identity.identity_id)
        return record

    async def on_event(self, bucket_id: str, event: Envelope) -> None:
        if event.bucket_id != bucket_id:
            raise PermissionError("bucket event ownership mismatch")
        if event.type == "bucket.status":
            self.pool.set_accepting(bucket_id, event.payload.get("accepting"))
            return
        with self.db.transaction():
            if identity_id := self.sessions.apply_request_event(event):
                self.pool.touch(bucket_id)
                self.pool.release(identity_id)


def create_app(config: ControlConfig) -> FastAPI:
    service = ControlService(config)
    pool = service.pool

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        reconciliation = asyncio.create_task(pool.run())
        try:
            yield
        finally:
            tasks = [reconciliation, *service.pending]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for socket in list(pool.sockets.values()):
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
        return {"status": "ready", "connected_buckets": sorted(pool.peers)}

    @app.websocket("/internal/buckets/ws")
    async def bucket_socket(socket: WebSocket):
        bucket_id = socket.headers.get("x-bucket-id", "")
        if not pool.active_bucket(bucket_id):
            await socket.close(code=1008)
            return
        await socket.accept()
        async def on_event(event: Envelope):
            if pool.peers.get(bucket_id) is not peer:
                raise PermissionError("superseded bucket connection")
            await service.on_event(bucket_id, event)

        peer = Peer(StarletteSocket(socket), service.db, bucket_id, on_event)
        async with pool.connection(bucket_id, peer, socket):
            try:
                await peer.run()
            except (WebSocketDisconnect, ConnectionError, RuntimeError, ValueError, TimeoutError):
                log.info("bucket connection ended bucket_id=%s", bucket_id)

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
        row = pool.active_bucket(bucket_id)
        if not row:
            raise HTTPException(404, "bucket not registered")
        return row

    @app.post("/internal/frontends/{frontend_id}/opencode/reset")
    async def reset_opencode(frontend_id: str):
        from fastapi.responses import JSONResponse
        result = await service.reset_frontend_opencode(frontend_id)
        return JSONResponse(result, status_code=207 if result["status"] == "partial_failure" else 200)

    return app
