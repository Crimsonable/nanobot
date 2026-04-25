from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any

from agent_gateway.config import DB_PATH


class BindingRepository:
    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = DB_PATH if db_path is None else db_path
        self._lock = threading.Lock()

    def init_db(self) -> None:
        path = self._db_path
        if isinstance(path, str):
            path = DB_PATH.__class__(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_bucket_binding (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    frontend_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    bucket_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(frontend_id, user_id)
                )
                """
            )
            conn.commit()

    @contextmanager
    def _conn(self) -> Any:
        path = self._db_path
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def get(self, frontend_id: str, user_id: str) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                """
                SELECT frontend_id, user_id, bucket_id, status, created_at, updated_at
                FROM user_bucket_binding
                WHERE frontend_id = ? AND user_id = ?
                """,
                (frontend_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """
                SELECT frontend_id, user_id, bucket_id, status, created_at, updated_at
                FROM user_bucket_binding
                WHERE user_id = ?
                ORDER BY frontend_id
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert(self, frontend_id: str, user_id: str, bucket_id: int) -> dict[str, Any]:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO user_bucket_binding (
                    frontend_id, user_id, bucket_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?)
                ON CONFLICT(frontend_id, user_id) DO UPDATE SET
                    bucket_id = excluded.bucket_id,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (frontend_id, user_id, bucket_id, now, now),
            )
            conn.commit()
        return self.get(frontend_id, user_id) or {
            "frontend_id": frontend_id,
            "user_id": user_id,
            "bucket_id": bucket_id,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }

    def count_by_bucket(self) -> dict[int, int]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """
                SELECT bucket_id, COUNT(*) AS total
                FROM user_bucket_binding
                WHERE status = 'active'
                GROUP BY bucket_id
                """
            ).fetchall()
        return {int(row["bucket_id"]): int(row["total"]) for row in rows}
