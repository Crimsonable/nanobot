"""Native transcript staging on the shared state volume; no OpenCode DB access."""
from __future__ import annotations

import json
import os
from pathlib import Path

from agent_backend.models import Identity


class SessionBusyError(RuntimeError):
    def __init__(self, bucket_id: str):
        super().__init__("session still has pending work")
        self.bucket_id = bucket_id


def check_owner(info: dict, identity: Identity, chat_id: str) -> None:
    context = info.get("metadata", {}).get("managed", {})
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


def verify_history(expected: dict, actual: dict) -> None:
    # Native import regenerates slug, project and bookkeeping timestamps.
    fields = ("id", "parentID", "title", "agent", "model", "metadata", "permissions")
    if (any(expected["info"].get(key) != actual["info"].get(key) for key in fields)
            or Path(expected["info"]["location"]["directory"]).resolve()
            != Path(actual["info"]["location"]["directory"]).resolve()
            or expected["messages"] != actual["messages"]):
        raise RuntimeError("native import history readback mismatch")
