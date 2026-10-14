"""Frontend session catalog and memory-only transfers between bucket databases."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3

from agent_backend.journal import Journal
from agent_backend.session_history import (
    delete_history, export_history, forget_local_session, import_history, validate_destination,
)

log = logging.getLogger(__name__)


class SessionManager:
    def __init__(self, service):
        self.service = service
        # Retain the only remaining copy across retryable failures. Deliberately
        # no transfer files, transfer IDs, or persistent migration state.
        self.histories = {}

    def root(self, identity, bucket_id):
        bucket = self.service.db.get("bucket", bucket_id)
        if not bucket or bucket["frontend_id"] != identity.frontend_id:
            raise PermissionError("session bucket frontend mismatch")
        if bucket_id in {".", ".."} or bucket_id != bucket["bucket_id"] or any(c in bucket_id for c in "/\\"):
            raise ValueError("invalid bucket ID")
        return self.service.config.runtime_for(identity.frontend_id).state_root / identity.frontend_id / bucket_id

    def lookup(self, submit):
        catalog = self.service.db.get("session", submit.session_key)
        if catalog is not None:
            return catalog
        # Existing installations only have bucket-local lookup rows. Discover a
        # conversation lazily once, so deploying capacity-based placement does
        # not strand the histories created before the central catalog existed.
        matches = []
        for bucket in self.service.db.records("bucket"):
            if bucket["frontend_id"] != submit.identity.frontend_id:
                continue
            path = self.root(submit.identity, bucket["bucket_id"]) / "runtime.sqlite"
            if not path.is_file():
                continue
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                row = db.execute("SELECT value FROM records WHERE kind='session' AND key=?",
                                 (submit.session_key,)).fetchone()
            finally:
                db.close()
            if row:
                local = json.loads(row[0])
                if local["identity_id"] != submit.identity.identity_id:
                    raise PermissionError("local session ownership mismatch")
                matches.append({"identity": submit.identity.model_dump(), "chat_id": submit.chat_id,
                    "session_id": local["session_id"], "bucket_id": bucket["bucket_id"]})
        if len(matches) > 1:
            raise RuntimeError("conversation has multiple source buckets")
        if matches:
            self.service.db.put("session", submit.session_key, matches[0])
            return matches[0]
        return None

    async def prepare(self, submit, target_bucket):
        catalog = self.lookup(submit)
        if catalog is None:
            return None
        if catalog["identity"] != submit.identity.model_dump() or catalog["chat_id"] != submit.chat_id:
            raise PermissionError("session catalog ownership mismatch")
        session_id, source_bucket = catalog["session_id"], catalog["bucket_id"]
        if source_bucket == target_bucket:
            return session_id
        pool = self.service.pool
        source_root = self.root(submit.identity, source_bucket)
        target_root = self.root(submit.identity, target_bucket)
        source_db = source_root / "opencode-home/data/opencode/opencode.db"
        target_db = target_root / "opencode-home/data/opencode/opencode.db"
        payload = {"identity": submit.identity.model_dump(), "chat_id": submit.chat_id,
                   "session_id": session_id}
        pool.pins[source_bucket] = pool.pins.get(source_bucket, 0) + 1
        try:
            bucket = self.service.db.get("bucket", source_bucket)
            if bucket["status"] == "draining":
                await pool.stopped.setdefault(source_bucket, asyncio.Event()).wait()
                bucket = self.service.db.get("bucket", source_bucket)
            online = bucket["status"] != "idle"
            if online:
                await pool.ready(source_bucket)
                await self.service.peers[source_bucket].session_call({**payload, "operation": "quiesce"})
            history = self.histories.get(submit.session_key)
            if history is None:
                history = export_history(source_db, session_id, submit.identity, submit.chat_id)
                validate_destination(target_db, history)
                self.histories[submit.session_key] = history
            # Once history is in RAM, immediately remove the original. Repeating
            # a delete after a lost reply is harmless; do not re-export stale data.
            while True:
                try:
                    if online:
                        await pool.ready(source_bucket)
                        await self.service.peers[source_bucket].session_call({**payload, "operation": "delete",
                            "session_ids": history.session_ids})
                    else:
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
                    break
                except (sqlite3.Error, ConnectionError, TimeoutError, RuntimeError):
                    log.exception("session import failed; retaining history in memory session_id=%s", session_id)
                    await asyncio.sleep(1)
            self.service.db.put("session", submit.session_key, {**catalog, "bucket_id": target_bucket})
            self.histories.pop(submit.session_key, None)
            return session_id
        finally:
            pool.pins[source_bucket] -= 1
