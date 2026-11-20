"""Frontend session catalog and memory-only transfers between bucket databases."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, closing
from pathlib import Path

from agent_backend.config import ControlConfig
from agent_backend.journal import Journal
from agent_backend.models import TERMINAL, Envelope, ExecutionStatus, Identity, Submit
from agent_backend.session_history import (
    delete_history, export_history, forget_local_session, import_history, validate_destination,
)

log = logging.getLogger(__name__)
SourceCall = Callable[[dict], Awaitable[bool]]


class SessionManager:
    def __init__(self, config: ControlConfig, db: Journal,
                 open_source: Callable[[str], AbstractContextManager[SourceCall]]):
        self.config = config
        self.db = db
        self.open_source = open_source
        self.active_migrations: dict[str, tuple[str, str]] = {}
        # Retain the only remaining copy across retryable failures. Deliberately
        # no transfer files, transfer IDs, or persistent migration state.
        self.histories = {}

    def can_reset(self, bucket_id: str) -> bool:
        """Admit a reset only after migrations, requests and their commands settle."""
        if any(bucket_id in buckets for buckets in self.active_migrations.values()) or self.db.pending(bucket_id):
            return False
        return not any(row["bucket_id"] == bucket_id and not row.get("result")
                       for row in self.db.records("request"))

    def record(self, submit: Submit, bucket_id: str, session_id: str) -> None:
        self.db.put("session", submit.session_key, {"identity": submit.identity.model_dump(),
                    "chat_id": submit.chat_id, "session_id": session_id, "bucket_id": bucket_id})

    def apply_request_event(self, event: Envelope) -> str | None:
        """Return the identity to release when a request first becomes terminal."""
        row = self.db.get("request", event.request_id)
        if not row or row["bucket_id"] != event.bucket_id:
            raise PermissionError("request does not belong to this bucket")
        namespace, _, status = event.type.partition(".")
        if namespace != "request":
            raise ValueError("unsupported bucket event")
        if status in {"accepted", "started"}:
            if not row.get("result"):
                if status == "started" and (session_id := event.payload.get("session_id")):
                    self.record(Submit.model_validate(row["submit"]), event.bucket_id, session_id)
                self.db.put("request", event.request_id, {**row, "status": status})
            return None
        if status not in TERMINAL:
            raise ValueError("unsupported bucket event")
        result = ExecutionStatus.model_validate(event.payload)
        if result.request_id != event.request_id or result.status != status:
            raise ValueError("terminal identity mismatch")
        if row.get("result"):
            if row["result"] != result.model_dump(mode="json"):
                raise ValueError("conflicting terminal result")
            return None
        return self._finish(row, result)

    def _finish(self, row: dict, result: ExecutionStatus) -> str:
        self.db.put("request", result.request_id, {**row, "status": result.status,
                    "result": result.model_dump(mode="json")})
        return Identity.model_validate(row["submit"]["identity"]).identity_id

    def interrupt_requests(self, bucket_id: str) -> set[str]:
        self.db.db.execute("DELETE FROM outbox WHERE peer=?", (bucket_id,))
        released = set()
        for row in self.db.records("request"):
            if row["bucket_id"] == bucket_id and not row.get("result"):
                result = ExecutionStatus(request_id=row["request_id"], status="failed",
                                         error="BUCKET_MISSING_EXECUTION_INTERRUPTED")
                released.add(self._finish(row, result))
        return released

    def root(self, identity: Identity, bucket_id: str) -> Path:
        bucket = self.db.get("bucket", bucket_id)
        if not bucket or bucket["frontend_id"] != identity.frontend_id:
            raise PermissionError("session bucket frontend mismatch")
        if bucket_id in {".", ".."} or bucket_id != bucket["bucket_id"] or any(c in bucket_id for c in "/\\"):
            raise ValueError("invalid bucket ID")
        return self.config.runtime_for(identity.frontend_id).state_root / identity.frontend_id / bucket_id

    def lookup(self, submit):
        catalog = self.db.get("session", submit.session_key)
        if catalog is not None:
            return catalog
        # Existing installations only have bucket-local lookup rows. Discover a
        # conversation lazily once, so deploying capacity-based placement does
        # not strand the histories created before the central catalog existed.
        matches = []
        for bucket in self.db.records("bucket"):
            if bucket["frontend_id"] != submit.identity.frontend_id:
                continue
            path = self.root(submit.identity, bucket["bucket_id"]) / "runtime.sqlite"
            if not path.is_file():
                continue
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute("SELECT value FROM records WHERE kind='session' AND key=?",
                                 (submit.session_key,)).fetchone()
            if row:
                local = json.loads(row[0])
                if local["identity_id"] != submit.identity.identity_id:
                    raise PermissionError("local session ownership mismatch")
                matches.append({"identity": submit.identity.model_dump(), "chat_id": submit.chat_id,
                    "session_id": local["session_id"], "bucket_id": bucket["bucket_id"]})
        if len(matches) > 1:
            raise RuntimeError("conversation has multiple source buckets")
        if matches:
            self.db.put("session", submit.session_key, matches[0])
            return matches[0]
        return None

    async def prepare(self, submit: Submit, target_bucket: str) -> str | None:
        if submit.content.strip().lower() == "/cancel":
            return None
        catalog = self.lookup(submit)
        if catalog is None:
            return None
        if catalog["identity"] != submit.identity.model_dump() or catalog["chat_id"] != submit.chat_id:
            raise PermissionError("session catalog ownership mismatch")
        session_id, source_bucket = catalog["session_id"], catalog["bucket_id"]
        if source_bucket == target_bucket:
            return session_id
        self.active_migrations[submit.session_key] = (source_bucket, target_bucket)
        try:
            with self.open_source(source_bucket) as call_if_online:
                await self._move(submit, session_id, source_bucket, target_bucket, call_if_online)
            self.record(submit, target_bucket, session_id)
            self.histories.pop(submit.session_key, None)
            return session_id
        finally:
            self.active_migrations.pop(submit.session_key)

    async def _move(self, submit: Submit, session_id: str, source_bucket: str,
                    target_bucket: str, call_if_online: SourceCall) -> None:
        source_root = self.root(submit.identity, source_bucket)
        target_root = self.root(submit.identity, target_bucket)
        source_db = source_root / "opencode-home/data/opencode/opencode.db"
        target_db = target_root / "opencode-home/data/opencode/opencode.db"
        payload = {"identity": submit.identity.model_dump(), "chat_id": submit.chat_id,
                   "session_id": session_id}
        online = await call_if_online({**payload, "operation": "quiesce"})
        history = self.histories.get(submit.session_key)
        if history is None:
            history = export_history(source_db, session_id, submit.identity, submit.chat_id)
            validate_destination(target_db, history)
            self.histories[submit.session_key] = history
        # A delete may have committed before its reply was lost. Keep using
        # the original in-memory snapshot across retries.
        while True:
            try:
                if online:
                    online = await call_if_online({**payload, "operation": "delete",
                                                   "session_ids": history.session_ids})
                if not online:
                    journal_path = source_root / "runtime.sqlite"
                    journal = Journal(journal_path) if journal_path.exists() else None
                    try:
                        delete_history(source_db, history)
                        if journal:
                            forget_local_session(journal, submit.session_key, history.session_ids)
                    finally:
                        if journal:
                            journal.close()
                import_history(target_db, history, submit.identity, submit.chat_id, target_bucket)
                return
            except (sqlite3.Error, ConnectionError, TimeoutError, RuntimeError):
                log.exception("session import failed; retaining history in memory session_id=%s", session_id)
                await asyncio.sleep(1)
