"""Single-writer SQLite journal. Transactions contain state changes AND outbox writes."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_backend.models import Envelope, Submit


class Journal:
    def __init__(self, path: Path, max_outbox: int = 10000):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lockfile = path.with_suffix(path.suffix + ".lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.lockfile.seek(0)
                if not self.lockfile.read(1):
                    self.lockfile.write(b"0")
                    self.lockfile.flush()
                self.lockfile.seek(0)
                msvcrt.locking(self.lockfile.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lockfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lockfile.close()
            raise RuntimeError("state directory already has an active writer") from None
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS records (
                kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(kind,key));
            CREATE TABLE IF NOT EXISTS outbox (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, peer TEXT NOT NULL,
                event TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox (
                peer TEXT NOT NULL, event_id TEXT NOT NULL, PRIMARY KEY(peer,event_id));
        """)
        self.max_outbox = max_outbox
        try:
            self.migrate_identifiers()
            self.migrate_chats()
        except BaseException:
            self.close()
            raise

    def migrate_identifiers(self) -> None:
        """Rename stored fields without changing identities, keys or workspace paths."""
        if self.db.execute("PRAGMA user_version").fetchone()[0] >= 1:
            return
        renames = {
            "binding": {"user_key": "identity_id"},
            "workspace_owner": {"user_key": "identity_id"},
            "session": {"user_key": "identity_id", "profile": "profile_id"},
            "artifact": {"owner": "identity_id"},
        }
        with self.transaction():
            for kind, fields in renames.items():
                for row in self.db.execute("SELECT key,value FROM records WHERE kind=?", (kind,)).fetchall():
                    value = json.loads(row["value"])
                    for old, new in fields.items():
                        if old in value:
                            if new in value and value[new] != value[old]:
                                raise ValueError(f"conflicting persisted {old}/{new}")
                            value[new] = value.pop(old)
                    self.put(kind, row["key"], value)
            self.db.execute("PRAGMA user_version=1")

    def migrate_chats(self) -> None:
        """Recover chat_id from CU records, including unacknowledged submissions."""
        if self.db.execute("PRAGMA user_version").fetchone()[0] >= 2:
            return

        def convert(payload: dict, chat_id: str) -> dict:
            payload = dict(payload)
            legacy_key = payload.pop("conversation_id")
            if "chat_id" in payload and payload["chat_id"] != chat_id:
                raise ValueError("conflicting persisted chat_id")
            payload["chat_id"] = chat_id
            if Submit.model_validate(payload).session_key != legacy_key:
                raise ValueError("persisted chat key mismatch")
            return payload

        with self.transaction():
            for row in self.db.execute("SELECT key,value FROM records WHERE kind='request'").fetchall():
                value = json.loads(row["value"])
                if "conversation_id" in value.get("submit", {}):
                    value["submit"] = convert(value["submit"], value["chat_id"])
                    self.put("request", row["key"], value)
            for row in self.db.execute("SELECT seq,event FROM outbox").fetchall():
                event = json.loads(row["event"])
                if event["type"] != "request.submit" or "conversation_id" not in event["payload"]:
                    continue
                request = self.get("request", event["request_id"])
                if request is None:
                    raise ValueError("legacy submission is missing its CU request record")
                event["payload"] = convert(event["payload"], request["chat_id"])
                self.db.execute("UPDATE outbox SET event=? WHERE seq=?",
                                (json.dumps(event, ensure_ascii=False), row["seq"]))
            # Bucket runs have only the old hash; Submit.from_record handles those
            # until a CU replay supplies chat_id. Session table keys stay unchanged.
            self.db.execute("PRAGMA user_version=2")

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT value FROM records WHERE kind=? AND key=?",
                              (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind: str, key: str, value: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?)",
                        (kind, key, json.dumps(value, ensure_ascii=False)))

    def records(self, kind: str) -> list[dict]:
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT value FROM records WHERE kind=? ORDER BY rowid", (kind,))]

    def delete(self, kind: str, key: str) -> None:
        self.db.execute("DELETE FROM records WHERE kind=? AND key=?", (kind, key))

    def enqueue(self, peer: str, event: Envelope) -> None:
        if self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] >= self.max_outbox:
            raise RuntimeError("OUTBOX_FULL")
        self.db.execute("INSERT INTO outbox(peer,event) VALUES (?,?)",
                        (peer, event.model_dump_json()))

    def pending(self, peer: str) -> list[Envelope]:
        return [Envelope.model_validate_json(row["event"]).model_copy(update={"seq": row["seq"]})
                for row in self.db.execute("SELECT * FROM outbox WHERE peer=? ORDER BY seq LIMIT 128",
                                           (peer,))]

    def ack(self, peer: str, seq: int, event_id: str) -> None:
        row = self.db.execute("SELECT event FROM outbox WHERE peer=? AND seq=?",
                              (peer, seq)).fetchone()
        if row and json.loads(row[0])["event_id"] == event_id:
            self.db.execute("DELETE FROM outbox WHERE peer=? AND seq=?", (peer, seq))

    def seen(self, peer: str, event_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM inbox WHERE peer=? AND event_id=?",
                               (peer, event_id)).fetchone() is not None

    def accept(self, peer: str, event_id: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO inbox VALUES (?,?)", (peer, event_id))

    def close(self) -> None:
        self.db.close()
        self.lockfile.close()
