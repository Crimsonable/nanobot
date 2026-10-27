from __future__ import annotations

import asyncio
import base64
import logging
from contextlib import asynccontextmanager
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from agent_backend.config import ControlConfig, GatewaySettings
from agent_backend.ingress import Inbound
from agent_backend.journal import Journal
from agent_backend.models import Envelope, Identity, OpenCodeEventChunk, Result, digest, uid
from agent_backend.wire import Peer
from channel_gateway.channel_service import ChannelService
from channel_gateway.native_reply import resolve_reply

log = logging.getLogger(__name__)


class ControlClient:
    def __init__(self, settings: GatewaySettings):
        self.http = httpx.AsyncClient(base_url=settings.control_url, timeout=180, trust_env=False)

    async def call(self, method, path, **kwargs):
        response = await self.http.request(method, path, **kwargs)
        if response.status_code < 500 and response.is_error:
            raise HTTPException(response.status_code, response.json().get("detail", "control rejected request"))
        response.raise_for_status()
        return response.json()

    async def submit(self, frontend, body):
        return await self.call("POST", f"/internal/submit/{quote(frontend, safe='')}", json=body)

    async def request(self, frontend, request_id):
        return await self.call("GET", f"/internal/requests/{quote(frontend, safe='')}/{quote(request_id, safe='')}")

    async def cancel(self, frontend, request_id):
        return await self.call("POST", f"/internal/requests/{quote(frontend, safe='')}/{quote(request_id, safe='')}/cancel")

    async def bucket(self, bucket_id):
        return await self.call("GET", f"/internal/buckets/{quote(bucket_id, safe='')}")

    async def close(self):
        await self.http.aclose()


class GatewayService:
    def __init__(self, config: ControlConfig, settings: GatewaySettings, channels=None, control=None):
        self.config, self.settings = config, settings
        self.db = Journal(settings.state_root / "gateway.sqlite", config.limits.outbox_messages)
        self.channels = channels or ChannelService(config)
        self.control = control or ControlClient(settings)
        self.delivery_ready = asyncio.Event()
        self.completed: dict[str, asyncio.Event] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.sockets: dict[str, WebSocket] = {}
        self.event_locks: dict[str, asyncio.Lock] = {}

    def require_frontend(self, frontend):
        if frontend not in self.config.frontends:
            raise HTTPException(404, "frontend is not registered")

    async def submit(self, frontend_id, inbound: Inbound, sync=False):
        self.require_frontend(frontend_id)
        request_id = inbound.request_id or uid("req_")
        inbound = inbound.model_copy(update={"request_id": request_id})
        fingerprint = digest({"frontend": frontend_id, "input": inbound.model_dump(), "sync": sync})
        async with self.locks.setdefault(request_id, asyncio.Lock()):
            row = self.db.get("request", request_id)
            legacy_match = row and row.get("legacy_hash") == digest({
                "identity": {"frontend_id": frontend_id, "user_id": inbound.user_id},
                "input": inbound.model_dump(exclude={"request_id"}), "sync": sync})
            if row and row["hash"] != fingerprint and not legacy_match:
                raise HTTPException(409, "request_id already used for different input")
            if not row:
                row = {"request_id": request_id, "hash": fingerprint, "frontend_id": frontend_id,
                       "input": inbound.model_dump(), "metadata": inbound.metadata, "chat_id": inbound.chat_id,
                       "submit": {"identity": {"frontend_id": frontend_id, "user_id": inbound.user_id}},
                       "sync": sync, "status": "queued", "delivery": "pending", "bucket_id": ""}
                self.db.put("request", request_id, row)
            if "rejected" in row:
                raise HTTPException(409, row["rejected"])
            if not row["bucket_id"]:
                try:
                    await self.dispatch(row)
                except httpx.HTTPError:
                    log.warning("submission durable; retrying request_id=%s", request_id)
            return self.db.get("request", request_id)

    async def dispatch(self, row):
        body = dict(row["input"])
        from channel_gateway.attachments import attachment_from_content_url
        auto = attachment_from_content_url(body["content"], self.config.frontends[row["frontend_id"]].attachment_url_prefix)
        if auto and not any((a.get("url") if isinstance(a, dict) else a) == auto["url"] for a in body["attachments"]):
            body["attachments"] = [*body["attachments"], auto]
        try:
            remote = await self.control.submit(row["frontend_id"], body)
        except HTTPException as exc:
            # Definitive rejection: never retry it as an uncertain transport failure.
            latest = self.db.get("request", row["request_id"])
            self.db.put("request", row["request_id"], {**latest, "rejected": exc.detail})
            raise
        latest = self.db.get("request", row["request_id"])
        self.db.put("request", row["request_id"], {**latest, "bucket_id": remote["bucket_id"],
                    "submit": remote["submit"]})
        await self.forward_cancel(row["request_id"])

    async def forward_cancel(self, request_id):
        row = self.db.get("request", request_id)
        if row.get("cancel_requested") and not row.get("cancel_forwarded") and row["bucket_id"] and not row.get("result"):
            await self.control.cancel(row["frontend_id"], request_id)
            latest = self.db.get("request", request_id)
            self.db.put("request", request_id, {**latest, "cancel_forwarded": True})

    async def recover_submissions(self):
        while True:
            for row in self.db.records("request"):
                if "rejected" not in row and (not row["bucket_id"] or (
                    row.get("cancel_requested") and not row.get("cancel_forwarded") and not row.get("result"))):
                    try:
                        async with self.locks.setdefault(row["request_id"], asyncio.Lock()):
                            latest = self.db.get("request", row["request_id"])
                            if not latest["bucket_id"]:
                                await self.dispatch(latest)
                            else:
                                await self.forward_cancel(row["request_id"])
                    except Exception:
                        log.warning("gateway submission pending request_id=%s", row["request_id"])
            await asyncio.sleep(1)

    async def event_request(self, bucket_id, event, frontend_id):
        row = self.db.get("request", event.request_id)
        if not row or row["frontend_id"] != frontend_id:
            raise PermissionError("unknown gateway request")
        if not row["bucket_id"]:
            remote = await self.control.request(frontend_id, event.request_id)
            row = {**row, "bucket_id": remote["bucket_id"], "submit": remote["submit"]}
            self.db.put("request", event.request_id, row)
        if row["bucket_id"] != bucket_id:
            raise PermissionError("request belongs to another bucket")
        return row

    async def on_event(self, bucket_id: str, event: Envelope, frontend_id: str):
        if event.bucket_id != bucket_id:
            raise PermissionError("bucket identity mismatch")
        if event.type == "opencode.event.chunk":
            await self.event_request(bucket_id, event, frontend_id)
            chunk = OpenCodeEventChunk.model_validate(event.payload)
            if chunk.index >= chunk.count or chunk.count > 1024:
                raise ValueError("invalid event chunk")
            key = digest([bucket_id, chunk.event_id])
            if self.db.get("opencode_event", key):
                return
            state = self.db.get("event_chunks", key) or {"count": chunk.count, "request_id": event.request_id, "parts": {}}
            if state["count"] != chunk.count or state["request_id"] != event.request_id:
                raise ValueError("chunk identity mismatch")
            index = str(chunk.index)
            if index in state["parts"] and state["parts"][index] != chunk.data:
                raise ValueError("conflicting chunk")
            state["parts"][index] = chunk.data
            self.db.put("event_chunks", key, state)
            if len(state["parts"]) == chunk.count:
                original = Envelope.model_validate_json(b"".join(base64.b64decode(state["parts"][str(i)], validate=True)
                                                                  for i in range(chunk.count)))
                if original.type != "opencode.event" or original.event_id != chunk.event_id or original.request_id != event.request_id:
                    raise ValueError("reassembled event identity mismatch")
                await self.on_event(bucket_id, original, frontend_id)
                self.db.delete("event_chunks", key)
            return
        row = await self.event_request(bucket_id, event, frontend_id)
        with self.db.transaction():
            if event.type == "opencode.event":
                payload = event.payload
                identity = row["submit"]["identity"]
                if any(payload.get(k) != v for k, v in {**identity, "chat_id": row["chat_id"]}.items()):
                    raise PermissionError("event routing mismatch")
                key = digest([bucket_id, event.event_id])
                if not self.db.get("opencode_event", key):
                    self.db.put("opencode_event", key, event.model_dump(mode="json"))
            elif event.type == "runtime.result":
                payload = dict(event.payload)
                source = payload.pop("native_reply", None)
                result = Result.model_validate(payload)
                if result.request_id != event.request_id:
                    raise ValueError("result identity mismatch")
                if source and result.status != "cancelled":
                    result = resolve_reply(self.db, bucket_id, source, result)
                if any(urlparse(a.url).scheme not in {"http", "https"} for a in result.attachments):
                    raise ValueError("published attachment requires an object URL")
                value = result.model_dump(mode="json")
                if row.get("result"):
                    return  # Replay cannot alter a result after later native events arrived.
                self.db.put("request", event.request_id, {**row, "status": result.status, "result": value})
                self.delivery_ready.set()
                self.completed.setdefault(event.request_id, asyncio.Event()).set()
            else:
                raise ValueError("unsupported gateway event")

    async def query(self, frontend, request_id):
        self.require_frontend(frontend)
        row = self.db.get("request", request_id)
        if not row or row["frontend_id"] != frontend:
            raise HTTPException(404, "request not found")
        if not row.get("result") and row["bucket_id"]:
            remote = await self.control.request(frontend, request_id)
            # Execution may finish before its output is received; final response waits for output.
            if remote["status"] not in {"completed", "failed", "cancelled"}:
                row = {**row, "status": remote["status"]}
        return row

    @staticmethod
    def response(row):
        identity = row["submit"]["identity"]
        return {"request_id": row["request_id"], "status": row["status"], **identity,
                "chat_id": row["chat_id"], "bucket_id": row["bucket_id"],
                "identity_id": Identity.model_validate(identity).identity_id, "metadata": row["metadata"],
                "delivery_state": row["delivery"], **(row.get("result") or {})}


    async def dispatch_channel(self, frontend_id: str, payload: dict) -> None:
        event = payload.get("event") or {}
        metadata = event.get("metadata") or {}
        message_id = metadata.get("message_id")
        request_id = "req_" + digest([frontend_id, event.get("user_id"), event.get("chat_id"), message_id]) if message_id else None
        try:
            await self.submit(frontend_id, Inbound(user_id=event["user_id"],
                chat_id=event.get("chat_id"), content=event.get("content") or "",
                attachments=event.get("attachments") or [], metadata=metadata, request_id=request_id))
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


def create_app(config: ControlConfig, settings: GatewaySettings, channels=None, control=None):
    service = GatewayService(config, settings, channels, control)

    @asynccontextmanager
    async def lifespan(app):
        await service.channels.start(service.dispatch_channel)
        tasks = [asyncio.create_task(service.deliveries()), asyncio.create_task(service.recover_submissions())]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for socket in list(service.sockets.values()):
                await socket.close()
            await service.channels.stop()
            await service.control.close()
            service.db.close()

    app = FastAPI(title="Channel gateway", lifespan=lifespan)
    app.state.service = service

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(exc)[:500]}, status_code=400)

    @app.exception_handler(httpx.HTTPError)
    async def unavailable(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "control unavailable; accepted requests remain queued"}, status_code=503)

    @app.get("/health/live")
    @app.get("/health/ready")
    async def health():
        return {"status": "ready"}

    @app.post("/inbound/{frontend_id}")
    async def inbound(frontend_id: str, body: Inbound):
        return service.response(await service.submit(frontend_id, body))

    @app.post("/inbound-sync/{frontend_id}")
    async def inbound_sync(frontend_id: str, body: Inbound):
        row = await service.submit(frontend_id, body, sync=True)
        ready = service.completed.setdefault(row["request_id"], asyncio.Event())
        if not row.get("result"):
            try:
                await asyncio.wait_for(ready.wait(), config.limits.request_seconds)
            except TimeoutError:
                raise HTTPException(504, {"request_id": row["request_id"], "error": "reply timeout"}) from None
        return service.response(service.db.get("request", row["request_id"]))

    @app.get("/requests/{frontend_id}/{request_id}")
    async def query(frontend_id: str, request_id: str):
        return service.response(await service.query(frontend_id, request_id))

    @app.post("/requests/{frontend_id}/{request_id}/cancel")
    async def cancel(frontend_id: str, request_id: str):
        service.require_frontend(frontend_id)
        row = service.db.get("request", request_id)
        if not row or row["frontend_id"] != frontend_id:
            raise HTTPException(404, "request not found")
        service.db.put("request", request_id, {**row, "cancel_requested": True})
        try:
            await service.forward_cancel(request_id)
        except httpx.HTTPError:
            log.warning("cancellation durable; retrying request_id=%s", request_id)
        return service.response(service.db.get("request", request_id))

    @app.post("/subscribe/{frontend_id}")
    async def subscribe(frontend_id: str, request: Request):
        from types import SimpleNamespace
        parser = service.channels.parsers.get(frontend_id)
        if parser is None or not parser.supports_subscribe():
            raise HTTPException(404, "subscription not available")
        value, payload = parser.process_subscribe_form(SimpleNamespace(**await request.json()))
        if payload is not None:
            await service.dispatch_channel(frontend_id, payload)
        return value

    @app.websocket("/internal/events/ws")
    async def events(socket: WebSocket):
        from agent_backend.sockets import StarletteSocket
        bucket_id = socket.headers.get("x-bucket-id", "")
        try:
            bucket = await service.control.bucket(bucket_id)
            service.require_frontend(bucket["frontend_id"])
        except (HTTPException, httpx.HTTPError):
            await socket.close(code=1008)
            return
        await socket.accept()
        if old := service.sockets.get(bucket_id):
            await old.close(code=1012)
        service.sockets[bucket_id] = socket

        async def receive(event):
            async with service.event_locks.setdefault(bucket_id, asyncio.Lock()):
                if service.sockets.get(bucket_id) is not socket:
                    raise PermissionError("superseded bucket connection")
                await service.on_event(bucket_id, event, bucket["frontend_id"])

        try:
            await Peer(StarletteSocket(socket), service.db, bucket_id, receive, protocol="agent-gateway/1").run()
        except (WebSocketDisconnect, ConnectionError, RuntimeError, ValueError, PermissionError, TimeoutError, httpx.HTTPError, HTTPException):
            log.info("gateway bucket disconnected bucket_id=%s", bucket_id)
        finally:
            if service.sockets.get(bucket_id) is socket:
                service.sockets.pop(bucket_id, None)

    return app
