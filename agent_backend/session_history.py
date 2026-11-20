"""Native transcript staging on the shared state volume; no OpenCode DB access."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from agent_backend.models import Identity


class SessionBusy(RuntimeError):
    def __init__(self, bucket_id: str):
        super().__init__("session still has pending work")
        self.bucket_id = bucket_id


def transfer_path(bucket_root: Path, source_bucket: str, session_key: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", source_bucket):
        raise ValueError("invalid source bucket")
    if not re.fullmatch(r"[a-f0-9]{64}", session_key):
        raise ValueError("invalid session key")
    return bucket_root.parent / source_bucket / "session-transfers" / (session_key + ".json")


def check_owner(info: dict, identity: Identity, chat_id: str) -> None:
    context = info.get("metadata", {}).get("bucket_runtime", {})
    if any(context.get(key) != value for key, value in {
        **identity.model_dump(), "chat_id": chat_id, "identity_id": identity.identity_id,
    }.items()):
        raise PermissionError("native session ownership mismatch")


def write_history(path: Path, sessions: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(sessions, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def read_history(path: Path, session_id: str, identity: Identity, chat_id: str) -> list[dict]:
    sessions = json.loads(path.read_text(encoding="utf-8"))
    if not sessions or sessions[0]["info"]["id"] != session_id:
        raise ValueError("transfer root mismatch")
    check_owner(sessions[0]["info"], identity, chat_id)
    seen = set()
    for data in sessions:
        info = data["info"]
        if info["id"] in seen or (seen and info.get("parentID") not in seen):
            raise ValueError("invalid session tree")
        seen.add(info["id"])
    return sessions


def imported_history(data: dict, bucket_id: str, context: dict, permissions: list[dict]) -> dict:
    info = data["info"]
    metadata = {**info.get("metadata", {}), "bucket_runtime": {
        **context, "schema_version": 1, "bucket_id": bucket_id, "requests": {},
    }}
    return {**data, "info": {**info, "metadata": metadata, "permissions": permissions}}


def verify_history(expected: dict, actual: dict) -> None:
    # Native import regenerates slug, location/project and bookkeeping timestamps.
    fields = ("id", "parentID", "title", "agent", "model", "metadata", "permissions")
    if (any(expected["info"].get(key) != actual["info"].get(key) for key in fields)
            or expected["messages"] != actual["messages"]):
        raise RuntimeError("native import history readback mismatch")
