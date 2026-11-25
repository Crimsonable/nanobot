from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request

from agent_backend.config import ControlConfig
from agent_backend.ingress import Inbound
from agent_backend.journal import Journal
from agent_backend.models import ExecutionStatus, Identity, Submit, digest, session_key, uid
from agent_backend.session_history import SessionBusyError
from container_up.bucket_manager import BucketMissingError
from container_up.bucket_scheduler import BucketScheduler
from container_up.migrations import upgrade_session_bindings
from container_up.session_manager import SessionManager

log = logging.getLogger(__name__)


class ControlService:
    def __init__(self, config: ControlConfig):
        self.config = config
        self.db = Journal(config.state_root / "control.sqlite")
        upgrade_session_bindings(self.db)
        self.session_locks: dict[str, asyncio.Lock] = {}
        self.pool = BucketScheduler(config, self.db, self.interrupt_bucket_requests)
        self.sessions = SessionManager(config, self.db, self.pool)
        self.pending: set[asyncio.Task] = set()

    def _shield(self, work):
        """Finish accepted submissions even if the HTTP caller disconnects."""
        task = asyncio.create_task(work)
        self.pending.add(task)

        def finished(done):
            self.pending.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return asyncio.shield(task)


    def interrupt_bucket_requests(self, bucket_id: str) -> None:
        with self.db.transaction():
            for key in self.sessions.interrupt_requests(bucket_id):
                self.pool.touch(bucket_id)
                self.pool.release(key)

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
        key = session_key(identity, inbound.chat_id)
        async with self.session_locks.setdefault(key, asyncio.Lock()):
            if existing := self._existing_request(request_id, fingerprint):
                await self.admit(existing)
                return self.db.get("request", request_id)
            submit = Submit(request_id=request_id, chat_id=inbound.chat_id, identity=identity,
                            content=inbound.content, attachments=inbound.attachment_references(),
                            metadata=inbound.metadata, stream=inbound.stream,
                            deadline_at=time.time() + self.config.limits.request_seconds)
            preferred_bucket = None
            while True:
                try:
                    async with self.pool.assignment(identity, inbound.chat_id, preferred_bucket) as binding:
                        # Another identity may have claimed the ID during bucket startup.
                        if existing := self._existing_request(request_id, fingerprint):
                            return existing
                        bucket_id = binding["bucket_id"]
                        workspace = Path(binding["workspace_path"])
                        if workspace.resolve() != workspace.absolute():
                            raise PermissionError("workspace cannot contain symbolic links")
                        workspace.mkdir(parents=True, exist_ok=True)
                        submit.session_id = await self.sessions.prepare(submit, bucket_id, workspace)
                        submit.deadline_at = time.time() + self.config.limits.request_seconds
                        await self.pool.ready(bucket_id)
                        row = self.register(submit, binding, fingerprint)
                        try:
                            await self.admit(row)
                        except httpx.HTTPError:
                            log.warning("native admission pending request_id=%s", request_id)
                        return self.db.get("request", request_id)
                except BucketMissingError:
                    continue
                except SessionBusyError as exc:
                    active = self.db.get("binding", key)
                    if active and active["bucket_id"] != exc.bucket_id:
                        raise HTTPException(409, "session has pending work in another bucket") from exc
                    preferred_bucket = exc.bucket_id

    def register(self, submit: Submit, binding: dict, fingerprint: str) -> dict:
        request_id = submit.request_id
        record = {"request_id": request_id, "hash": fingerprint, "bucket_id": binding["bucket_id"],
                  "chat_id": submit.chat_id,
                  "submit": submit.model_dump(mode="json"), "status": "queued",
                  "native": {"session_id": submit.session_id, "message_id": uid("msg_"),
                             "directory": binding["workspace_path"]}}
        with self.db.transaction():
            if self.db.get("request", request_id):
                raise HTTPException(409, "concurrent duplicate request; query the existing request")
            if self.db.get("binding", submit.session_key) != binding:
                raise RuntimeError("bucket binding changed during request submission")
            self.db.put("request", request_id, record)
        log.info("request queued bucket_id=%s request_id=%s identity_id=%s", record["bucket_id"], request_id, submit.identity.identity_id)
        return record

    async def admit(self, row):
        row = self.db.get("request", row["request_id"])
        if row.get("result") or row.get("admitted"):
            return
        if row.get("cancel_requested") or row["submit"]["deadline_at"] <= time.time():
            await self.cancel(row["request_id"], expired=not row.get("cancel_requested"))
            return
        submit = Submit.model_validate(row["submit"])
        if submit.content.strip().lower() == "/cancel":
            for other in self.db.records("request"):
                if other["request_id"] != row["request_id"] and not other.get("result") and Submit.model_validate(other["submit"]).session_key == submit.session_key:
                    await self.cancel(other["request_id"])
            self.complete(ExecutionStatus(request_id=row["request_id"], status="completed"))
            return
        client = await self.pool.client(row["bucket_id"])
        text = submit.content
        if submit.attachments:
            text += "\n\nAttachments:\n" + "\n".join(
                f"{a.filename} ({a.content_type}): {a.url or a.path}" +
                (" (requires authorized access)" if a.requires_auth else "") for a in submit.attachments)
        await client.submit(row["native"], text, {"request_id": submit.request_id,
            "deadline_at": submit.deadline_at})
        self.db.put("request", row["request_id"], {**self.db.get("request", row["request_id"]),
                    "admitted": True, "status": "accepted"})

    def complete(self, result: ExecutionStatus):
        row = self.db.get("request", result.request_id)
        if row.get("result"):
            return
        key = self.sessions.finish(row, result)
        self.pool.touch(row["bucket_id"])
        self.pool.release(key)

    async def cancel(self, request_id, *, expired=False):
        row = self.db.get("request", request_id)
        if row.get("result"):
            return
        expired = row.get("cancel_expired", expired)
        self.db.put("request", request_id, {**row, "cancel_requested": True, "cancel_expired": expired})
        client = await self.pool.client(row["bucket_id"])
        await client.cancel_input(row["native"])
        self.complete(ExecutionStatus(request_id=request_id, status="failed" if expired else "cancelled",
                                     error="REQUEST_TIMEOUT" if expired else None))

    async def recover(self):
        async def recover_request(row):
            try:
                key = Submit.model_validate(row["submit"]).session_key
                async with self.session_locks.setdefault(key, asyncio.Lock()):
                    row = self.db.get("request", row["request_id"])
                    if row.get("result"):
                        return
                    if row.get("cancel_requested") or row["submit"]["deadline_at"] <= time.time():
                        await self.cancel(row["request_id"], expired=not row.get("cancel_requested"))
                    elif not row.get("admitted"):
                        await self.admit(row)
            except Exception:
                log.warning("native request recovery pending request_id=%s", row["request_id"], exc_info=True)

        while True:
            await asyncio.gather(*(recover_request(row) for row in self.db.records("request")
                                   if not row.get("result")))
            await asyncio.sleep(1)


def create_app(config: ControlConfig) -> FastAPI:
    service = ControlService(config)
    pool = service.pool

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        reconciliation = asyncio.create_task(pool.run())
        recovery = asyncio.create_task(service.recover())
        try:
            yield
        finally:
            tasks = [reconciliation, recovery, *service.pending]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await pool.close()
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
        return {"status": "ready", "connected_buckets": [b["bucket_id"] for b in service.db.records("bucket") if pool.connected(b["bucket_id"])]}

    @app.post("/internal/submit/{frontend_id}")
    async def submit(frontend_id: str, body: Inbound):
        return await service.submit(frontend_id, body)

    @app.get("/internal/requests/{frontend_id}/{request_id}")
    async def query(frontend_id: str, request_id: str):
        service.require_frontend(frontend_id)
        row = service.db.get("request", request_id)
        if not row or row["submit"]["identity"]["frontend_id"] != frontend_id:
            raise HTTPException(404, "request not found")
        return service.db.get("request", request_id)

    @app.post("/internal/requests/{frontend_id}/{request_id}/cancel")
    async def cancel(frontend_id: str, request_id: str):
        row = await query(frontend_id, request_id)
        key = Submit.model_validate(row["submit"]).session_key
        async with service.session_locks.setdefault(key, asyncio.Lock()):
            await service.cancel(request_id)
        return service.db.get("request", request_id)

    @app.post("/internal/requests/{frontend_id}/{request_id}/collected")
    async def collected(frontend_id: str, request_id: str, result: ExecutionStatus):
        await query(frontend_id, request_id)
        if result.request_id != request_id:
            raise ValueError("result identity mismatch")
        service.complete(result)
        row = service.db.get("request", request_id)
        service.db.put("request", request_id, {**row, "output_collected": True})
        pool.release(Submit.model_validate(row["submit"]).session_key)
        return {"status": "ok"}

    @app.get("/internal/buckets/{bucket_id}")
    async def bucket(bucket_id: str):
        row = pool.active_bucket(bucket_id)
        if not row:
            raise HTTPException(404, "bucket not registered")
        return row


    return app
