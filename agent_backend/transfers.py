from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

from agent_backend.journal import Journal
from agent_backend.models import Attachment, digest
from agent_backend.wire import CHUNK_SIZE, Peer


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Transfers:
    """Resumable, owner-bound disk spool. Never accepts a remote filesystem path."""

    def __init__(self, root: Path, journal: Journal, file_limit: int, spool_limit: int):
        self.root, self.journal = root, journal
        self.file_limit, self.spool_limit = file_limit, spool_limit
        root.mkdir(parents=True, exist_ok=True)
        self.locks: dict[str, asyncio.Lock] = {}

    def path(self, transfer_id: str) -> Path:
        return self.root / digest(transfer_id)

    def begin(self, owner: str, args: dict) -> dict:
        transfer_id = str(args["transfer_id"])
        attachment = Attachment.model_validate(args["attachment"])
        if attachment.size > self.file_limit or len(attachment.sha256) != 64:
            raise ValueError("invalid file size or hash")
        existing = self.journal.get("transfer", transfer_id)
        identity = {"owner": owner, "request_id": args["request_id"],
                    "attachment": attachment.model_dump(mode="json")}
        if existing:
            if any(existing[k] != v for k, v in identity.items()):
                raise ValueError("transfer identity mismatch")
            path = self.path(transfer_id)
            return {"offset": path.stat().st_size if path.exists() else 0}
        reserved = sum(r["attachment"]["size"] for r in self.journal.records("transfer")
                       if not r.get("released"))
        if reserved + attachment.size > self.spool_limit:
            raise RuntimeError("SPOOL_FULL")
        self.path(transfer_id).touch(exist_ok=False)
        self.journal.put("transfer", transfer_id, {**identity, "transfer_id": transfer_id})
        return {"offset": 0}

    def record(self, owner: str, transfer_id: str) -> dict:
        row = self.journal.get("transfer", transfer_id)
        if not row or row["owner"] != owner:
            raise PermissionError("unknown transfer")
        return row

    def chunk(self, owner: str, args: dict, data: bytes | None) -> dict:
        row = self.record(owner, args["transfer_id"])
        if data is None or len(data) > CHUNK_SIZE or row.get("released"):
            raise ValueError("invalid chunk")
        offset = int(args["offset"])
        path = self.path(args["transfer_id"])
        size = path.stat().st_size
        if offset != size or size + len(data) > row["attachment"]["size"]:
            raise ValueError("unexpected chunk offset or size")
        with path.open("ab") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return {"offset": size + len(data)}

    def verify(self, owner: str, transfer_id: str) -> tuple[dict, Path]:
        row = self.record(owner, transfer_id)
        path = self.path(transfer_id)
        if (path.stat().st_size != row["attachment"]["size"]
                or file_hash(path) != row["attachment"]["sha256"]):
            raise ValueError("attachment checksum mismatch")
        return row, path

    def release(self, owner: str, transfer_id: str) -> None:
        row = self.record(owner, transfer_id)
        self.path(transfer_id).unlink(missing_ok=True)
        self.journal.put("transfer", transfer_id, {**row, "released": True})


async def send_file(peer: Peer, path: Path, attachment: Attachment, request_id: str,
                    transfer_id: str, prefix: str = "attachment") -> dict:
    response = await peer.call(prefix + ".begin", {
        "transfer_id": transfer_id, "request_id": request_id,
        "attachment": attachment.model_dump(mode="json"),
    })
    if "result" in response:
        return response["result"]
    offset = response["offset"]
    with path.open("rb") as stream:
        stream.seek(offset)
        while chunk := stream.read(CHUNK_SIZE):
            response = await peer.call(prefix + ".chunk", {
                "transfer_id": transfer_id, "offset": offset,
            }, chunk)
            offset = response["offset"]
    return await peer.call(prefix + ".commit", {"transfer_id": transfer_id})
