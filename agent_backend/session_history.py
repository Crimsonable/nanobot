"""In-memory moves of native OpenCode history on the shared local state PVC.

This adapter targets the pinned OpenCode SQLite schema. It copies native rows,
including structured message parts, rather than replaying prompts. Each import
is one transaction, so failed imports cannot leave half a transcript behind.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from agent_backend.models import Identity

TABLES = ("session", "message", "part", "todo", "session_message",
          "session_input", "session_context_epoch")


@contextmanager
def database(path: Path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def owner(row: dict, identity: Identity, chat_id: str) -> None:
    context = json.loads(row.get("metadata") or "{}").get("bucket_runtime", {})
    if any(context.get(key) != value for key, value in {
        **identity.model_dump(), "chat_id": chat_id, "identity_id": identity.identity_id,
    }.items()):
        raise PermissionError("native session ownership mismatch")


@dataclass
class History:
    rows: dict[str, list[dict]]
    projects: list[dict]

    @property
    def session_ids(self) -> list[str]:
        return [row["id"] for row in self.rows["session"]]


def export_history(path: Path, session_id: str, identity: Identity, chat_id: str) -> History:
    with database(path) as db:
        db.execute("BEGIN")
        root = db.execute('SELECT * FROM session WHERE id=?', (session_id,)).fetchone()
        if root is None:
            raise RuntimeError("session history is missing from its recorded bucket")
        owner(dict(root), identity, chat_id)
        sessions = [dict(root)]
        seen = {session_id}
        for session in sessions:
            for row in db.execute('SELECT * FROM session WHERE parent_id=?', (session["id"],)):
                if row["id"] not in seen:
                    seen.add(row["id"])
                    sessions.append(dict(row))
        for row in sessions:
            if row["directory"] != root["directory"]:
                raise PermissionError("child session workspace mismatch")
        ids = tuple(row["id"] for row in sessions)
        marks = ",".join("?" for _ in ids)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"session", "message", "part", "project"} <= tables:
            raise RuntimeError("unsupported OpenCode history schema")
        rows = {"session": sessions}
        for table in TABLES[1:]:
            if table in tables:
                rows[table] = [dict(row) for row in db.execute(
                    f'SELECT * FROM "{table}" WHERE session_id IN ({marks})', ids)]
        projects = [dict(db.execute('SELECT * FROM project WHERE id=?', (pid,)).fetchone())
                    for pid in dict.fromkeys(row["project_id"] for row in sessions)]
        return History(rows, projects)


def validate_destination(path: Path, history: History) -> None:
    with database(path) as db:
        for table, rows in {"project": history.projects, **history.rows}.items():
            columns = {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')}
            if not columns or any(set(row) != columns for row in rows):
                raise RuntimeError("OpenCode history schemas differ: " + table)


def delete_rows(db: sqlite3.Connection, ids: list[str]) -> None:
    marks = ",".join("?" for _ in ids)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in reversed(TABLES[1:]):
        if table in tables:
            db.execute(f'DELETE FROM "{table}" WHERE session_id IN ({marks})', ids)
    db.execute(f'DELETE FROM session WHERE id IN ({marks})', ids)


def delete_history(path: Path, history: History) -> None:
    with database(path) as db:
        db.execute("BEGIN IMMEDIATE")
        delete_rows(db, history.session_ids)


def import_history(path: Path, history: History, identity: Identity, chat_id: str, bucket_id: str) -> None:
    with database(path) as db:
        db.execute("BEGIN IMMEDIATE")
        # An interrupted previous attempt may have committed before CU updated
        # its catalog. Replace only this conversation, never another user's data.
        for session_id in history.session_ids:
            existing = db.execute('SELECT * FROM session WHERE id=?', (session_id,)).fetchone()
            if existing is not None:
                owner(dict(existing), identity, chat_id)
        delete_rows(db, history.session_ids)
        for table, rows in {"project": history.projects, **history.rows}.items():
            for original in rows:
                row = dict(original)
                if table == "session":
                    metadata = json.loads(row.get("metadata") or "{}")
                    metadata["bucket_runtime"] = {
                        **metadata.get("bucket_runtime", {}), "schema_version": 1,
                        **identity.model_dump(), "identity_id": identity.identity_id,
                        "chat_id": chat_id, "bucket_id": bucket_id, "requests": {},
                    }
                    metadata["bucket_runtime"].pop("input_message_id", None)
                    row["metadata"] = json.dumps(metadata, ensure_ascii=False)
                columns = ",".join('"' + column + '"' for column in row)
                marks = ",".join("?" for _ in row)
                conflict = ' ON CONFLICT(id) DO NOTHING' if table == "project" else ""
                db.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({marks}){conflict}', tuple(row.values()))


def forget_local_session(journal, session_key: str, session_ids: list[str]) -> None:
    """Discard local lookup/reducer copies, but retain delivery and audit records."""
    with journal.transaction():
        journal.delete("session", session_key)
        for row in journal.db.execute("SELECT key,value FROM records WHERE kind='message'").fetchall():
            if json.loads(row["value"]).get("session_id") in session_ids:
                journal.delete("message", row["key"])
        for session_id in session_ids:
            journal.delete("child_session", session_id)
