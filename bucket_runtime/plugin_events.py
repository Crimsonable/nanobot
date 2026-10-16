"""Durable native event intake, attribution and consumption owned by runtime."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path

from agent_backend.models import Envelope, OpenCodeEvent, PluginEvent, Submit, digest
from agent_backend.wire import CHUNK_SIZE
from bucket_runtime.session_logger import SessionLogger

log = logging.getLogger(__name__)


class PluginEvents:
    def __init__(self, runtime):
        self.runtime = runtime
        self.changed = asyncio.Event()
        self.logger = SessionLogger(runtime.config.logging_file)

    @property
    def db(self):
        return self.runtime.db

    def accept(self, body: dict) -> dict:
        event = PluginEvent.model_validate(body)
        if not isinstance(event.event.get("type"), str):
            raise ValueError("native event type is required")
        with self.db.transaction():
            if self.db.seen("plugin", event.event_id):
                return {"ok": True, "duplicate": True}
            self.db.put("plugin_pending", event.event_id, event.model_dump(mode="json"))
            self.db.accept("plugin", event.event_id)
        self.changed.set()
        return {"ok": True}

    def attribute(self, raw: dict) -> tuple[OpenCodeEvent, dict]:
        native = raw["event"]
        kind, props = native["type"], native.get("properties", {})
        info, part = props.get("info") or {}, props.get("part") or {}
        session = (props.get("sessionID") or part.get("sessionID") or info.get("sessionID")
                   or (info.get("id") if kind.startswith("session.") else "") or "")
        key = digest([raw["directory"], session])
        state = self.db.get("native_session", key) or {}
        if kind in {"session.created", "session.updated"}:
            context = info.get("metadata", {}).get("bucket_runtime", {})
            if context.get("schema_version") == 1:
                state["context"] = context
            if info.get("parentID"):
                state["parent"] = info["parentID"]
        message = (props.get("messageID") or part.get("messageID")
                   or (info.get("id") if kind == "message.updated" else "") or "")
        if part.get("callID"):
            self.db.put("native_call", digest([key, part["callID"]]), {"message": message})
        if not message and props.get("callID"):
            message = (self.db.get("native_call", digest([key, props["callID"]])) or {}).get("message", "")
        message_key = digest([session, message])
        if kind == "message.updated":
            # Routing identifiers only; never persist message text, parts or deltas.
            self.db.put("native_message_route", message_key, {
                "id": info.get("id"), "role": info.get("role"), "parentID": info.get("parentID")})
        message_info = self.db.get("native_message_route", message_key) or {}
        input_id = (message_info.get("id") if message_info.get("role") == "user"
                    else message_info.get("parentID")) or ""
        if kind == "message.updated" and info.get("role") == "user":
            state["active_input"] = info["id"]
        if kind == "session.error":
            input_id = state.get("active_input", "")
        context = state.get("context", {})
        parent = state.get("parent")
        seen = {session}
        while not context and parent and parent not in seen:
            seen.add(parent)
            ancestor = self.db.get("native_session", digest([raw["directory"], parent])) or {}
            context, parent = ancestor.get("context", {}), ancestor.get("parent")
        rows = [r for r in self.db.records("run") if r.get("session_id") == session]
        row = next((r for r in rows if input_id and r.get("message_id") == input_id), None)
        owner_row = row or (rows[-1] if rows else None)
        if owner_row is None:
            # Child sessions share the parent's business chat, including when the
            # parent has no metadata event in this runtime's received stream.
            ancestors = [r for r in self.db.records("run") if r.get("session_id") in seen - {session}]
            owner_row = ancestors[-1] if ancestors else None
        owner = {}
        if owner_row:
            if Path(raw["directory"]).resolve() != Path(owner_row["directory"]).resolve():
                raise PermissionError("native event workspace mismatch")
            submit = Submit.model_validate(owner_row["submit"])
            owner = {**submit.identity.model_dump(), "chat_id": submit.chat_id}
        elif context:
            from agent_backend.models import Identity
            from agent_backend.workspaces import workspace_path
            identity = Identity(frontend_id=context["frontend_id"], user_id=context["user_id"])
            if (identity.frontend_id != self.runtime.config.frontend_id or
                    Path(raw["directory"]).resolve() != workspace_path(
                        self.runtime.config.workspace_root, identity).resolve()):
                raise PermissionError("native session ownership mismatch")
            owner = {**identity.model_dump(), "chat_id": context["chat_id"]}
        if kind == "session.idle":
            state.pop("active_input", None)
        if session:
            self.db.put("native_session", key, state)
        return OpenCodeEvent(**raw, session_id=session, message_id=message,
                             input_message_id=input_id, request_id=row["request_id"] if row else ""), owner

    def process_next(self) -> bool:
        # SELECT only the head; avoid loading the entire durable queue per event.
        record = self.db.db.execute(
            "SELECT value FROM records WHERE kind='plugin_pending' ORDER BY rowid LIMIT 1").fetchone()
        if record is None:
            return False
        raw = json.loads(record[0])
        try:
            with self.db.transaction():
                event, owner = self.attribute(raw)
                line = self.logger.format(event, owner)
                envelope = Envelope(type="opencode.event", event_id=event.event_id,
                    bucket_id=self.runtime.config.bucket_id, request_id=event.request_id,
                    payload={**event.model_dump(mode="json"), **owner})
                encoded = envelope.model_dump_json().encode()
                if len(encoded) <= CHUNK_SIZE * 2:
                    self.db.enqueue(self.runtime.config.bucket_id, envelope)
                else:
                    count = (len(encoded) + CHUNK_SIZE - 1) // CHUNK_SIZE
                    for index in range(count):
                        data = encoded[index * CHUNK_SIZE:(index + 1) * CHUNK_SIZE]
                        self.db.enqueue(self.runtime.config.bucket_id, Envelope(type="opencode.event.chunk",
                            bucket_id=self.runtime.config.bucket_id, request_id=event.request_id, payload={
                                "event_id": event.event_id, "index": index, "count": count,
                                "data": base64.b64encode(data).decode("ascii")}))
                self.runtime.reduce_event(event)
                self.db.delete("plugin_pending", event.event_id)
        except RuntimeError as exc:
            if str(exc) == "OUTBOX_FULL":
                return False  # Backpressure: retain ordered input until CU drains the outbox.
            raise
        except (ValueError, KeyError, TypeError, AttributeError, PermissionError) as exc:
            # A malformed event cannot hold all later sessions hostage. Keep it for inspection.
            with self.db.transaction():
                self.db.put("plugin_failed", raw["event_id"], {"body": raw, "error": str(exc)})
                self.db.delete("plugin_pending", raw["event_id"])
            log.warning("native event retained after processing failure: %s", raw["event_id"])
            return True
        if line is not None:
            self.logger.write(line)
        return True

    async def run(self):
        while True:
            self.changed.clear()
            if self.process_next():
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=1)
            except TimeoutError:
                pass
