"""Single-process durable results, independent of the caller's HTTP connection."""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from mcp.server.fastmcp.exceptions import ToolError


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class AnalysisHistory:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL UNIQUE,
            user_id TEXT NOT NULL, chat_id TEXT NOT NULL, query TEXT NOT NULL,
            attachments TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, result TEXT, error TEXT
        )""")
        self.db.execute("CREATE INDEX IF NOT EXISTS conversation ON analyses(user_id, chat_id, id)")
        self.db.execute(
            "UPDATE analyses SET status='unknown', error=?, updated_at=? WHERE status='running'",
            ("MCP 服务重启，未收到最终结果；后端可能仍在执行，请勿自动重复提交。", timestamp()),
        )
        self.db.commit()
        self.tasks = {}

    async def analyze(self, service, user_id, chat_id, query, attachments):
        key = (user_id, chat_id)
        if key in self.tasks:
            task, original = self.tasks[key]
            if original != (query, attachments):
                raise ToolError("此会话已有分析正在执行，请先调用 query_analysis_result 查询结果。")
        else:
            now = timestamp()
            request_id = uuid4().hex
            self.db.execute(
                """INSERT INTO analyses
                (request_id,user_id,chat_id,query,attachments,status,created_at,updated_at)
                VALUES (?,?,?,?,?,'running',?,?)""",
                (request_id, user_id, chat_id, query, json.dumps(attachments), now, now),
            )
            self.db.commit()
            task = asyncio.create_task(
                self._run(service, request_id, user_id, chat_id, query, attachments)
            )
            self.tasks[key] = (task, (query, attachments.copy()))
            task.add_done_callback(lambda done: self._finished(key, done))
        # MCP cancellation/disconnection must not cancel the upstream request.
        return await asyncio.shield(task)

    def _finished(self, key, task):
        if self.tasks.get(key, (None,))[0] is task:
            self.tasks.pop(key)
        if not task.cancelled():
            task.exception()  # Consume errors even when the original caller has disconnected.

    def _update(self, request_id, status, result=None, error=None):
        self.db.execute(
            "UPDATE analyses SET status=?,result=?,error=?,updated_at=? WHERE request_id=?",
            (status, result, error, timestamp(), request_id),
        )
        self.db.commit()

    async def _run(self, service, request_id, user_id, chat_id, query, attachments):
        try:
            result = await service.analyze(user_id, chat_id, query, attachments)
            self._update(request_id, "completed", result.model_dump_json())
            return result
        except asyncio.CancelledError:
            self._update(request_id, "unknown", error="MCP 服务停止，结果未知；后端可能仍在执行。")
            raise
        except Exception as exc:
            # Transport failures do not establish whether the backend executed the request.
            error = str(exc) if isinstance(exc, ToolError) else "分析调用异常，请检查 MCP 服务日志。"
            self._update(request_id, "unknown", error=error)
            raise ToolError(error + " 可调用 query_analysis_result 查看本地记录，请勿自动重复提交。") from None

    def query(self, user_id, chat_id, limit=20, before_id=None):
        rows = self.db.execute(
            """SELECT * FROM analyses WHERE user_id=? AND chat_id=? AND (? IS NULL OR id<?)
            ORDER BY id DESC LIMIT ?""",
            (user_id, chat_id, before_id, before_id, limit + 1),
        ).fetchall()
        more = len(rows) > limit
        records = []
        for row in rows[:limit]:
            record = dict(row)
            record["attachments"] = json.loads(record["attachments"])
            record["result"] = json.loads(record["result"]) if record["result"] else None
            records.append(record)
        return {
            "user_id": user_id, "chat_id": chat_id,
            "status": records[0]["status"] if records else "not_found",
            "records": records,
            "next_before_id": records[-1]["id"] if more else None,
        }

    async def close(self):
        tasks = [item[0] for item in self.tasks.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.db.close()
