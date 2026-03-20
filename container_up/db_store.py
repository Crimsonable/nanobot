from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any

from container_up.settings import DB_PATH

db_lock = threading.Lock()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS org_routes (
                org_id TEXT PRIMARY KEY,
                container_name TEXT NOT NULL,
                container_id TEXT NOT NULL,
                bridge_url TEXT NOT NULL,
                bridge_token TEXT NOT NULL,
                workspace_path TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_active_at TEXT NOT NULL
            )
            """
        )
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(org_routes)").fetchall()
        }
        if "last_active_at" not in columns:
            conn.execute(
                f"ALTER TABLE org_routes ADD COLUMN last_active_at TEXT NOT NULL DEFAULT '{now}'"
            )
        conn.commit()


@contextmanager
def db_conn() -> Any:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def list_org_records() -> list[dict[str, Any]]:
    with db_lock, db_conn() as conn:
        rows = conn.execute("SELECT * FROM org_routes ORDER BY org_id").fetchall()
    return [dict(row) for row in rows]


def org_record(org_id: str) -> dict[str, Any] | None:
    with db_lock, db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM org_routes WHERE org_id = ?",
            (org_id,),
        ).fetchone()
    return dict(row) if row else None


def upsert_org_record(record: dict[str, Any]) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    last_active_at = record.get("last_active_at", now)
    with db_lock, db_conn() as conn:
        conn.execute(
            """
            INSERT INTO org_routes (
                org_id, container_name, container_id, bridge_url, bridge_token,
                workspace_path, status, created_at, updated_at, last_active_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(org_id) DO UPDATE SET
                container_name=excluded.container_name,
                container_id=excluded.container_id,
                bridge_url=excluded.bridge_url,
                bridge_token=excluded.bridge_token,
                workspace_path=excluded.workspace_path,
                status=excluded.status,
                updated_at=excluded.updated_at,
                last_active_at=excluded.last_active_at
            """,
            (
                record["org_id"],
                record["container_name"],
                record["container_id"],
                record["bridge_url"],
                record["bridge_token"],
                record["workspace_path"],
                record["status"],
                record.get("created_at", now),
                now,
                last_active_at,
            ),
        )
        conn.commit()


def touch_org(org_id: str) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with db_lock, db_conn() as conn:
        conn.execute(
            "UPDATE org_routes SET last_active_at = ?, updated_at = ? WHERE org_id = ?",
            (now, now, org_id),
        )
        conn.commit()


def delete_org_record(org_id: str) -> None:
    with db_lock, db_conn() as conn:
        conn.execute("DELETE FROM org_routes WHERE org_id = ?", (org_id,))
        conn.commit()


def count_org_records() -> int:
    with db_lock, db_conn() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM org_routes").fetchone()[0])
