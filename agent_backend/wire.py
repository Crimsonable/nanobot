"""Durable ordered events over WebSocket; reconnect replays unacknowledged events."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from agent_backend.journal import Journal
from agent_backend.models import Envelope

CHUNK_SIZE = 256 * 1024


class Socket(Protocol):
    async def send(self, data: str) -> None: ...
    async def recv(self) -> str: ...


class Peer:
    def __init__(self, socket: Socket, journal: Journal, bucket_id: str,
                 handler: Callable[[Envelope], Awaitable[None]],
                 session_handler: Callable[[dict], Awaitable[None]] | None = None,
                 journal_peer: str | None = None, protocol: str = "agent-bucket/1"):
        self.socket, self.journal, self.bucket_id = socket, journal, bucket_id
        self.handler = handler
        self.journal_peer = journal_peer or bucket_id
        self.protocol = protocol
        self.send_lock = asyncio.Lock()
        self.closed = False
        self.session_handler = session_handler
        self.session_calls: dict[str, asyncio.Future] = {}

    async def session_call(self, payload: dict) -> None:
        """Small, memory-only migration controls, serialized by the CU user lock.

        The existing native session ID correlates replies; no migration IDs or
        durable transfer messages are created. History stays on the shared PVC.
        """
        key = payload["session_id"]
        if self.closed:
            raise ConnectionError("bucket connection is closed")
        if key in self.session_calls:
            raise RuntimeError("session operation already pending")
        future = asyncio.get_running_loop().create_future()
        self.session_calls[key] = future
        try:
            await self.send(Envelope(type="session.call", bucket_id=self.bucket_id,
                                     request_id=key, payload=payload))
            # A live operation must finish before reusing this session's key.
            # Native HTTP calls have their own timeout; disconnect fails all
            # pending calls below. A local timer could misattribute a late reply
            # to the next delete/quiesce on the same session.
            await future
        finally:
            self.session_calls.pop(key, None)

    async def send(self, event: Envelope) -> None:
        async with self.send_lock:
            await self.socket.send(event.model_copy(update={"protocol": self.protocol}).model_dump_json())

    async def receive(self) -> None:
        while True:
            packet = await asyncio.wait_for(self.socket.recv(), 60)
            if not isinstance(packet, str) or len(packet.encode()) > 1024 * 1024:
                raise ValueError("invalid control frame")
            event = Envelope.model_validate_json(packet)
            if event.bucket_id != self.bucket_id or event.protocol != self.protocol:
                raise ValueError("bucket identity mismatch")
            if event.type == "ack":
                self.journal.ack(self.journal_peer, event.payload["seq"], event.payload["event_id"])
            elif event.type == "heartbeat":
                continue
            elif event.type == "session.reply":
                future = self.session_calls.get(event.request_id)
                if future is not None and not future.done():
                    if error := event.payload.get("error"):
                        future.set_exception(RuntimeError(error))
                    else:
                        future.set_result(None)
            elif event.type == "session.call":
                if self.session_handler is None or event.request_id != event.payload.get("session_id"):
                    raise ValueError("unsupported session operation")
                try:
                    await self.session_handler(event.payload)
                    result = {}
                except Exception as exc:
                    result = {"error": str(exc)}
                await self.send(Envelope(type="session.reply", bucket_id=self.bucket_id,
                                         request_id=event.request_id, payload=result))
            else:
                if not event.seq:
                    raise ValueError("durable messages require sequence")
                if not self.journal.seen(self.journal_peer, event.event_id):
                    await self.handler(event)
                    self.journal.accept(self.journal_peer, event.event_id)
                await self.send(Envelope(type="ack", bucket_id=self.bucket_id,
                    payload={"seq": event.seq, "event_id": event.event_id}))

    async def pump(self) -> None:
        notification = self.journal.notification(self.journal_peer)
        sent = 0
        while True:
            notification.clear()
            while events := self.journal.pending(self.journal_peer, after=sent):
                for event in events:
                    await self.send(event)
                    sent = event.seq
            await notification.wait()

    async def heartbeat(self) -> None:
        while True:
            await self.send(Envelope(type="heartbeat", bucket_id=self.bucket_id))
            await asyncio.sleep(20)

    async def run(self) -> None:
        tasks = [asyncio.create_task(fn()) for fn in (self.receive, self.pump, self.heartbeat)]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.closed = True
            for future in self.session_calls.values():
                if not future.done():
                    future.set_exception(ConnectionError("bucket disconnected during session operation"))
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
