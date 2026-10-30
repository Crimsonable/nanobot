"""Request-scoped durable SSE log; no interpretation of native message parts."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from agent_backend.journal import Journal

log = logging.getLogger(__name__)


def encode_event(name: str, data: dict, seq: int | None = None) -> str:
    prefix = f"id: {seq}\n" if seq is not None else ""
    return f"{prefix}event: {name}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


class RequestStreams:
    def __init__(self, db: Journal):
        self.db = db
        self.listeners: dict[str, set[asyncio.Event]] = {}
        self.db.db.execute("""
            CREATE TABLE IF NOT EXISTS stream_events (
                request_id TEXT NOT NULL, seq INTEGER NOT NULL,
                name TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(request_id, seq))
        """)

    def notify(self, request_id: str) -> None:
        for listener in self.listeners.get(request_id, ()):
            listener.set()

    def last_seq(self, request_id: str) -> int:
        return self.db.db.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM stream_events WHERE request_id=?",
            (request_id,)).fetchone()[0]

    def append(self, request_id: str, name: str, data: dict) -> None:
        # Called in the same transaction as intake/deduplication or request result.
        # No await before commit: listeners can only read committed entries.
        with self.db.transaction():
            self.db.db.execute("INSERT INTO stream_events VALUES (?, ?, ?, ?)",
                (request_id, self.last_seq(request_id) + 1, name,
                 json.dumps(data, ensure_ascii=False, separators=(",", ":"))))
        self.notify(request_id)

    async def iterate(self, request_id: str, after: int, timeout: float,
                      heartbeat: float = 15) -> AsyncIterator[str]:
        listener = asyncio.Event()
        self.listeners.setdefault(request_id, set()).add(listener)
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            while True:
                # Subscribe before reading, and clear before the query, so events
                # arriving between replay and waiting cannot be missed.
                listener.clear()
                entries = self.db.db.execute(
                    "SELECT seq, name, data FROM stream_events "
                    "WHERE request_id=? AND seq>? ORDER BY seq LIMIT 64",
                    (request_id, after)).fetchall()
                for entry in entries:
                    after = entry["seq"]
                    yield f'id: {after}\nevent: {entry["name"]}\ndata: {entry["data"]}\n\n'
                    if entry["name"] == "gateway.result":
                        return
                if entries:
                    # Yield to intake even when a replay client drains immediately.
                    await asyncio.sleep(0)
                    continue
                row = self.db.get("request", request_id)
                if row.get("result"):
                    return  # The caller already acknowledged the terminal entry.
                if "rejected" in row:
                    yield encode_event("gateway.error", {"request_id": request_id,
                        "code": "REQUEST_REJECTED", "message": str(row["rejected"])})
                    return
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    yield encode_event("gateway.error", {"request_id": request_id,
                        "code": "STREAM_WAIT_TIMEOUT", "message": "等待超时，请查询请求状态"})
                    return
                try:
                    await asyncio.wait_for(listener.wait(), min(heartbeat, remaining))
                except TimeoutError:
                    if asyncio.get_running_loop().time() < deadline:
                        yield ": ping\n\n"
        except Exception:
            log.exception("SSE interrupted request_id=%s", request_id)
            yield encode_event("gateway.error", {"request_id": request_id,
                "code": "STREAM_ERROR", "message": "事件流中断，请使用原请求编号重试或查询"})
        finally:
            listeners = self.listeners[request_id]
            listeners.discard(listener)
            if not listeners:
                del self.listeners[request_id]
