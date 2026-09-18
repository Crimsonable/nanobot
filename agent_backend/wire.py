"""Reliable control messages plus bounded RPC/chunk transfers on one WebSocket.

The receiver dispatches RPC handlers independently, so a handler may await an RPC
in the opposite direction. Durable command handlers must commit before returning.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from agent_backend.journal import Journal
from agent_backend.models import Envelope, uid

log = logging.getLogger(__name__)
CHUNK_SIZE = 256 * 1024
HEADER = struct.Struct("!H")


class Socket(Protocol):
    async def send(self, data: str | bytes) -> None: ...
    async def recv(self) -> str | bytes: ...


class Peer:
    def __init__(self, socket: Socket, journal: Journal, bucket_id: str,
                 handler: Callable[[Envelope], Awaitable[None]],
                 rpc: Callable[[str, dict, bytes | None], Awaitable[dict]]):
        self.socket, self.journal, self.bucket_id = socket, journal, bucket_id
        self.handler, self.rpc_handler = handler, rpc
        self.waiters: dict[str, asyncio.Future] = {}
        self.send_lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self.last_received = 0.0
        self.closed = False

    async def send(self, event: Envelope, data: bytes | None = None) -> None:
        encoded = event.model_dump_json()
        packet: str | bytes = encoded
        if data is not None:
            raw = encoded.encode()
            if len(data) > CHUNK_SIZE or len(raw) > 65535:
                raise ValueError("frame too large")
            packet = HEADER.pack(len(raw)) + raw + data
        async with self.send_lock:
            await self.socket.send(packet)

    async def call(self, method: str, payload: dict, data: bytes | None = None,
                   timeout: float = 120) -> dict:
        if self.closed:
            raise ConnectionError("bucket disconnected")
        correlation = uid()
        future = asyncio.get_running_loop().create_future()
        self.waiters[correlation] = future
        try:
            try:
                await self.send(Envelope(type="rpc", bucket_id=self.bucket_id,
                                         correlation_id=correlation,
                                         payload={"method": method, "args": payload}), data)
            except Exception as exc:
                raise ConnectionError("RPC transport interrupted") from exc
            return await asyncio.wait_for(future, timeout)
        finally:
            self.waiters.pop(correlation, None)

    async def respond(self, event: Envelope, data: bytes | None) -> None:
        try:
            result = await self.rpc_handler(event.payload["method"], event.payload["args"], data)
            payload = {"result": result}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("RPC failed method=%s error=%s", event.payload.get("method"), type(exc).__name__)
            payload = {"error": str(exc)[:500]}
        await self.send(Envelope(type="rpc.result", bucket_id=self.bucket_id,
                                 correlation_id=event.correlation_id, payload=payload))

    async def receive(self) -> None:
        while True:
            packet = await asyncio.wait_for(self.socket.recv(), 60)
            if len(packet) > 1024 * 1024:
                raise ValueError("control frame exceeds limit")
            self.last_received = asyncio.get_running_loop().time()
            data = None
            if isinstance(packet, bytes):
                if len(packet) < 2:
                    raise ValueError("invalid binary frame")
                length = HEADER.unpack(packet[:2])[0]
                data = packet[2 + length:]
                if length > len(packet) - 2 or len(data) > CHUNK_SIZE:
                    raise ValueError("invalid binary frame length")
                packet = packet[2:2 + length].decode()
            event = Envelope.model_validate_json(packet)
            if event.bucket_id != self.bucket_id:
                raise ValueError("bucket identity mismatch")
            if event.type == "ack":
                self.journal.ack(self.bucket_id, event.payload["seq"], event.payload["event_id"])
            elif event.type == "heartbeat":
                continue
            elif event.type == "rpc.result":
                future = self.waiters.get(event.correlation_id)
                if future and not future.done():
                    if "error" in event.payload:
                        future.set_exception(RuntimeError(event.payload["error"]))
                    else:
                        future.set_result(event.payload["result"])
            elif event.type == "rpc":
                if len(self.tasks) >= 64:
                    raise RuntimeError("too many in-flight RPCs")
                task = asyncio.create_task(self.respond(event, data))
                self.tasks.add(task)
                task.add_done_callback(self._task_done)
            else:
                if not event.seq or data is not None:
                    raise ValueError("durable messages require sequence")
                if not self.journal.seen(self.bucket_id, event.event_id):
                    await self.handler(event)
                    self.journal.accept(self.bucket_id, event.event_id)
                await self.send(Envelope(type="ack", bucket_id=self.bucket_id,
                                         payload={"seq": event.seq, "event_id": event.event_id}))

    def _task_done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.warning("RPC response interrupted: %s", type(task.exception()).__name__)

    async def pump(self) -> None:
        heartbeat_at = 0.0
        while True:
            for event in self.journal.pending(self.bucket_id):
                await self.send(event)
            if time.monotonic() - heartbeat_at >= 20:
                await self.send(Envelope(type="heartbeat", bucket_id=self.bucket_id))
                heartbeat_at = time.monotonic()
            await asyncio.sleep(1)

    async def run(self) -> None:
        receive = asyncio.create_task(self.receive())
        pump = asyncio.create_task(self.pump())
        try:
            done, _ = await asyncio.wait([receive, pump], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.closed = True
            for future in self.waiters.values():
                if not future.done():
                    future.set_exception(ConnectionError("bucket disconnected"))
            for task in [receive, pump, *self.tasks]:
                task.cancel()
            await asyncio.gather(receive, pump, *self.tasks, return_exceptions=True)
