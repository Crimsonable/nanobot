from __future__ import annotations

import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from container_up.settings import (
    BUCKET_IDLE_TTL_SECONDS,
    BUCKET_MAX_INSTANCES_PER_BUCKET,
    BUCKET_NAME_PREFIX,
    BUCKET_NAMESPACE,
    BUCKET_SERVICE_PORT,
    DB_PATH,
    build_bucket_base_url,
)


class BindingRepository:
    """SQLite-backed runtime state for user instances and bucket capacity."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path).expanduser() if db_path is not None else DB_PATH
        self._lock = threading.RLock()

    def init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_instances (
                    user_id TEXT PRIMARY KEY,
                    workspace_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    bucket_id TEXT,
                    instance_id TEXT,
                    frontend_id TEXT,
                    app_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_active_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS buckets (
                    bucket_id TEXT PRIMARY KEY,
                    bucket_name TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_instances INTEGER NOT NULL DEFAULT 0,
                    max_instances INTEGER NOT NULL,
                    service_host TEXT,
                    service_port INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_instances_status ON user_instances(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_buckets_status ON buckets(status, current_instances)"
            )
            conn.commit()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction_immediate(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def get_user_instance(self, user_id: str) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            return self._get_user_instance(conn, user_id)

    def get_bucket(self, bucket_id: str) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            return self._get_bucket(conn, bucket_id)

    def list_buckets(self) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """
                SELECT bucket_id, bucket_name, namespace, status, current_instances,
                       max_instances, service_host, service_port, created_at, updated_at
                FROM buckets
                ORDER BY created_at ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_idle_buckets_ready_for_scale_down(self) -> list[dict[str, Any]]:
        cutoff = time.time() - BUCKET_IDLE_TTL_SECONDS
        cutoff_text = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff))
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """
                SELECT bucket_id, bucket_name, namespace, status, current_instances,
                       max_instances, service_host, service_port, created_at, updated_at
                FROM buckets
                WHERE status = 'idle'
                  AND current_instances = 0
                  AND updated_at < ?
                ORDER BY updated_at ASC
                """,
                (cutoff_text,),
            ).fetchall()
        return [dict(row) for row in rows]

    def touch_bucket(self, bucket_id: str, *, status: str | None = None) -> dict[str, Any] | None:
        with self.transaction_immediate() as conn:
            if status is None:
                conn.execute(
                    "UPDATE buckets SET updated_at = ? WHERE bucket_id = ?",
                    (_utc_now(), bucket_id),
                )
            else:
                conn.execute(
                    "UPDATE buckets SET status = ?, updated_at = ? WHERE bucket_id = ?",
                    (status, _utc_now(), bucket_id),
                )
            return self._get_bucket(conn, bucket_id)

    def reserve_user_instance(
        self,
        *,
        user_id: str,
        workspace_path: str,
        frontend_id: str | None,
        app_id: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        with self.transaction_immediate() as conn:
            existing = self._get_user_instance(conn, user_id)
            if existing is not None and existing.get("status") == "online" and existing.get("bucket_id"):
                bucket = self._get_bucket(conn, str(existing["bucket_id"]))
                if bucket is not None:
                    return existing, bucket, False
            if existing is not None and existing.get("status") == "creating" and existing.get("bucket_id"):
                bucket = self._get_bucket(conn, str(existing["bucket_id"]))
                if bucket is not None:
                    return existing, bucket, True

            bucket = self._find_available_bucket(conn)
            if bucket is None:
                bucket = self._create_next_bucket_record(conn)

            self._increment_bucket_instances(conn, str(bucket["bucket_id"]))
            bucket = self._get_bucket(conn, str(bucket["bucket_id"]))
            if bucket is None:
                raise RuntimeError("reserved bucket record disappeared")

            now = _utc_now()
            instance_id = user_id
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO user_instances (
                        user_id, workspace_path, status, bucket_id, instance_id,
                        frontend_id, app_id, created_at, updated_at, last_active_at
                    ) VALUES (?, ?, 'creating', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        workspace_path,
                        bucket["bucket_id"],
                        instance_id,
                        frontend_id,
                        app_id,
                        now,
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE user_instances
                    SET workspace_path = ?, status = 'creating', bucket_id = ?, instance_id = ?,
                        frontend_id = ?, app_id = ?, updated_at = ?, last_active_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        workspace_path,
                        bucket["bucket_id"],
                        instance_id,
                        frontend_id,
                        app_id,
                        now,
                        now,
                        user_id,
                    ),
                )
            user = self._get_user_instance(conn, user_id)
            if user is None:
                raise RuntimeError("reserved user instance record disappeared")
            return user, bucket, True

    def mark_user_instance_online(self, user_id: str) -> dict[str, Any]:
        with self.transaction_immediate() as conn:
            now = _utc_now()
            conn.execute(
                """
                UPDATE user_instances
                SET status = 'online', updated_at = ?, last_active_at = ?
                WHERE user_id = ?
                """,
                (now, now, user_id),
            )
            user = self._get_user_instance(conn, user_id)
            if user is None or not user.get("bucket_id"):
                raise RuntimeError(f"user instance not found when marking online: {user_id}")
            self._refresh_bucket_status(conn, str(user["bucket_id"]))
            user = self._get_user_instance(conn, user_id)
            if user is None:
                raise RuntimeError(f"user instance disappeared when marking online: {user_id}")
            return user

    def rollback_user_instance_reservation(self, user_id: str, bucket_id: str) -> None:
        with self.transaction_immediate() as conn:
            user = self._get_user_instance(conn, user_id)
            if user is not None:
                now = _utc_now()
                conn.execute(
                    """
                    UPDATE user_instances
                    SET status = 'error', bucket_id = NULL, instance_id = NULL, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (now, user_id),
                )
            self._decrement_bucket_instances(conn, bucket_id)
            self._refresh_bucket_status(conn, bucket_id)

    def release_user_instance(
        self,
        user_id: str,
        *,
        bucket_id: str | None = None,
        instance_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self.transaction_immediate() as conn:
            user = self._get_user_instance(conn, user_id)
            if user is None or user.get("status") != "online":
                return user
            current_bucket_id = str(user.get("bucket_id") or "")
            current_instance_id = str(user.get("instance_id") or "")
            if bucket_id and bucket_id != current_bucket_id:
                return user
            if instance_id and instance_id != current_instance_id:
                return user

            now = _utc_now()
            conn.execute(
                """
                UPDATE user_instances
                SET status = 'destroyed', bucket_id = NULL, instance_id = NULL, updated_at = ?
                WHERE user_id = ?
                """,
                (now, user_id),
            )
            if current_bucket_id:
                self._decrement_bucket_instances(conn, current_bucket_id)
                self._refresh_bucket_status(conn, current_bucket_id)
            return self._get_user_instance(conn, user_id)

    def touch_user_activity(self, user_id: str) -> None:
        with self.transaction_immediate() as conn:
            user = self._get_user_instance(conn, user_id)
            if user is None:
                return
            now = _utc_now()
            conn.execute(
                """
                UPDATE user_instances
                SET updated_at = ?, last_active_at = ?
                WHERE user_id = ?
                """,
                (now, now, user_id),
            )
            bucket_id = str(user.get("bucket_id") or "")
            if bucket_id:
                conn.execute(
                    "UPDATE buckets SET updated_at = ? WHERE bucket_id = ?",
                    (now, bucket_id),
                )

    def get(self, frontend_id: str, user_id: str) -> dict[str, Any] | None:
        user = self.get_user_instance(user_id)
        if user is None:
            return None
        current_frontend = str(user.get("frontend_id") or "").strip()
        if current_frontend and current_frontend != frontend_id:
            return None
        return user

    def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        user = self.get_user_instance(user_id)
        return [user] if user is not None else []

    def _get_user_instance(self, conn: sqlite3.Connection, user_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT user_id, workspace_path, status, bucket_id, instance_id,
                   frontend_id, app_id, created_at, updated_at, last_active_at
            FROM user_instances
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        return dict(row) if row else None

    def _get_bucket(self, conn: sqlite3.Connection, bucket_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT bucket_id, bucket_name, namespace, status, current_instances,
                   max_instances, service_host, service_port, created_at, updated_at
            FROM buckets
            WHERE bucket_id = ?
            """,
            (bucket_id,),
        ).fetchone()
        return dict(row) if row else None

    def _find_available_bucket(self, conn: sqlite3.Connection) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT bucket_id, bucket_name, namespace, status, current_instances,
                   max_instances, service_host, service_port, created_at, updated_at
            FROM buckets
            WHERE status IN ('creating', 'running', 'idle')
              AND current_instances < max_instances
            ORDER BY current_instances ASC, created_at ASC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None

    def _create_next_bucket_record(self, conn: sqlite3.Connection) -> dict[str, Any]:
        next_index = 0
        rows = conn.execute("SELECT bucket_id FROM buckets").fetchall()
        for row in rows:
            bucket_id = str(row["bucket_id"])
            match = re.search(r"(\d+)$", bucket_id)
            if match:
                next_index = max(next_index, int(match.group(1)) + 1)

        bucket_id = f"bucket-{next_index}"
        bucket_name = f"{BUCKET_NAME_PREFIX}-{next_index}"
        now = _utc_now()
        conn.execute(
            """
            INSERT INTO buckets (
                bucket_id, bucket_name, namespace, status, current_instances,
                max_instances, service_host, service_port, created_at, updated_at
            ) VALUES (?, ?, ?, 'creating', 0, ?, ?, ?, ?, ?)
            """,
            (
                bucket_id,
                bucket_name,
                BUCKET_NAMESPACE,
                BUCKET_MAX_INSTANCES_PER_BUCKET,
                build_bucket_base_url(bucket_id, bucket_name),
                BUCKET_SERVICE_PORT,
                now,
                now,
            ),
        )
        bucket = self._get_bucket(conn, bucket_id)
        if bucket is None:
            raise RuntimeError(f"failed to create bucket record: {bucket_id}")
        return bucket

    def _increment_bucket_instances(self, conn: sqlite3.Connection, bucket_id: str) -> None:
        now = _utc_now()
        conn.execute(
            """
            UPDATE buckets
            SET current_instances = current_instances + 1,
                updated_at = ?
            WHERE bucket_id = ?
            """,
            (now, bucket_id),
        )

    def _decrement_bucket_instances(self, conn: sqlite3.Connection, bucket_id: str) -> None:
        now = _utc_now()
        conn.execute(
            """
            UPDATE buckets
            SET current_instances = CASE
                    WHEN current_instances > 0 THEN current_instances - 1
                    ELSE 0
                END,
                updated_at = ?
            WHERE bucket_id = ?
            """,
            (now, bucket_id),
        )

    def _refresh_bucket_status(self, conn: sqlite3.Connection, bucket_id: str) -> None:
        bucket = self._get_bucket(conn, bucket_id)
        if bucket is None:
            return
        current = int(bucket["current_instances"])
        maximum = int(bucket["max_instances"])
        if current <= 0:
            status = "idle"
        elif current >= maximum:
            status = "full"
        else:
            status = "running"
        conn.execute(
            "UPDATE buckets SET status = ?, updated_at = ? WHERE bucket_id = ?",
            (status, _utc_now(), bucket_id),
        )


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
