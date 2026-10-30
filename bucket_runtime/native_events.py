"""Durable native event intake, attribution and consumption owned by runtime."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from uuid import uuid4

import httpx

from agent_backend.models import Envelope, NativeEvent, OpenCodeEvent, Submit, digest
from bucket_runtime.session_logger import SessionLogger

log = logging.getLogger(__name__)


class NativeEvents:
    def __init__(self, runtime):
        self.runtime = runtime
        self.changed = asyncio.Event()
        self.connected = asyncio.Event()
        self.logger = SessionLogger(runtime.config.logging_file)
        # Preserve already accepted events when upgrading from plugin transport.
        with self.db.transaction():
            for old, new in [("plugin_pending", "native_pending"), ("plugin_failed", "native_failed")]:
                self.db.db.execute("UPDATE records SET kind=? WHERE kind=?", (new, old))
            self.db.db.execute("INSERT OR IGNORE INTO inbox SELECT 'opencode', event_id FROM inbox WHERE peer='plugin'")
            self.db.db.execute("DELETE FROM inbox WHERE peer='plugin'")

    @property
    def db(self):
        return self.runtime.db

    async def subscribe(self):
        """Reconnect the server stream; never resubmit prompts after a stream gap."""
        while True:
            try:
                async for frame in self.runtime.client.events():
                    native = frame["payload"]
                    kind = native["type"]
                    if kind == "server.connected":
                        self.connected.set()
                    # Heartbeats carry no business data and need no durable delivery.
                    if kind == "server.heartbeat":
                        continue
                    while self.db.db.execute("SELECT COUNT(*) FROM records WHERE kind='native_pending'").fetchone()[0] >= self.runtime.config.limits.outbox_messages:
                        await asyncio.sleep(0.1)
                    self.accept({"type": "opencode.event", "event_id": native.get("id") or uuid4().hex,
                        "directory": frame.get("directory") or "global", "event": native})
                log.warning("OpenCode SSE ended; reconnecting (missed events cannot be replayed)")
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                log.warning("OpenCode SSE disconnected (%s); reconnecting (missed events cannot be replayed)",
                            type(exc).__name__)
            finally:
                self.connected.clear()
            await asyncio.sleep(1)

    def accept(self, body: dict) -> dict:
        if self.runtime.intake_closed:
            return {"ok": True, "ignored": True}
        event = NativeEvent.model_validate(body)
        if not isinstance(event.event.get("type"), str):
            raise ValueError("native event type is required")
        with self.db.transaction():
            if self.db.seen("opencode", event.event_id):
                return {"ok": True, "duplicate": True}
            self.db.put("native_pending", event.event_id, event.model_dump(mode="json"))
            self.db.accept("opencode", event.event_id)
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
        if kind in {"session.error", "session.status", "session.idle"}:
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
        if row is None and owner_row and kind in {"session.status", "session.error", "session.idle"}:
            if owner_row["status"] == "running":
                row = owner_row
        if row is None and owner_row and session != owner_row.get("session_id"):
            row = owner_row
        if row is None and owner_row and owner_row["status"] == "running" and not message_info:
            row = owner_row
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
        if session:
            self.db.put("native_session", key, state)
        return OpenCodeEvent(**raw, session_id=session, message_id=message,
                             input_message_id=input_id, request_id=row["request_id"] if row else ""), owner

    def forward(self, event) -> bool:
        row = self.db.get("run", event.request_id)
        if row["submit"].get("stream", False):
            # Attribution/workspace checks still run before this policy. Keep all
            # attributable native JSON, including unknown types and child sessions.
            return True
        from agent_backend.config import EventForwarding
        policy = EventForwarding.model_validate(row.get("event_forwarding") or self.runtime.config.event_forwarding.model_dump())
        kind, props = event.event["type"], event.event.get("properties", {})
        part = props.get("part") or {}
        part_key = digest([event.session_id, event.message_id, part.get("id") or props.get("partID")])
        if part.get("type"):
            self.db.put("native_part_type", part_key, {"type": part["type"]})
        if kind not in policy.include_types:
            return False
        if not policy.include_child_sessions and event.session_id != row.get("session_id"):
            return False
        if kind.startswith("message.part."):
            part_type = part.get("type") or (self.db.get("native_part_type", part_key) or {}).get("type")
            return part_type in policy.include_part_types
        return True

    def process_next(self) -> bool:
        # SELECT only the head; avoid loading the entire durable queue per event.
        record = self.db.db.execute(
            "SELECT value FROM records WHERE kind='native_pending' ORDER BY rowid LIMIT 1").fetchone()
        if record is None:
            return False
        raw = json.loads(record[0])
        try:
            with self.db.transaction():
                event, owner = self.attribute(raw)
                line = self.logger.format(event, owner)
                if owner and event.request_id and self.forward(event):
                    envelope = Envelope(type="opencode.event", event_id=event.event_id,
                        bucket_id=self.runtime.config.bucket_id, request_id=event.request_id,
                        payload={**event.model_dump(mode="json"), **owner})
                    self.runtime.gateway_emit(envelope)
                self.runtime.reduce_event(event)
                self.db.delete("native_pending", event.event_id)
        except RuntimeError as exc:
            if str(exc) == "OUTBOX_FULL":
                return False  # Backpressure: retain ordered input until CU drains the outbox.
            raise
        except (ValueError, KeyError, TypeError, AttributeError, PermissionError) as exc:
            # A malformed event cannot hold all later sessions hostage. Keep it for inspection.
            with self.db.transaction():
                self.db.put("native_failed", raw["event_id"], {"body": raw, "error": str(exc)})
                self.db.delete("native_pending", raw["event_id"])
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
