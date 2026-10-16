import asyncio
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from hazard_expert_mcp.history import AnalysisHistory
from hazard_expert_mcp.service import AnalysisResult


def result():
    return AnalysisResult(status="completed", user_id="u", chat_id="c", content="答案", attachments=[])


async def test_disconnect_does_not_cancel_upstream_and_result_survives_restart(tmp_path):
    path = str(tmp_path / "history.sqlite3")
    history = AnalysisHistory(path)
    started, release = asyncio.Event(), asyncio.Event()

    async def analyze(*args):
        started.set()
        await release.wait()
        return result()

    service = AsyncMock()
    service.analyze.side_effect = analyze
    caller = asyncio.create_task(history.analyze(service, "u", "c", "问题", []))
    await started.wait()
    assert history.query("u", "c")["status"] == "running"
    assert history.query("other", "c")["status"] == "not_found"
    assert history.query("u", "other")["status"] == "not_found"
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not history.tasks[("u", "c")][0].cancelled()
    # A concurrent duplicate joins the existing operation, a new round is rejected.
    with pytest.raises(ToolError, match="已有分析"):
        await history.analyze(service, "u", "c", "另一个问题", [])
    duplicate = asyncio.create_task(history.analyze(service, "u", "c", "问题", []))
    await asyncio.sleep(0)
    release.set()
    assert (await duplicate).content == "答案"
    service.analyze.assert_awaited_once()
    await history.close()
    reopened = AnalysisHistory(path)
    assert reopened.query("u", "c")["records"][0]["result"]["content"] == "答案"
    await reopened.close()


async def test_unknown_result_does_not_return_previous_round_as_latest(tmp_path):
    history = AnalysisHistory(str(tmp_path / "history.sqlite3"))
    service = AsyncMock()
    service.analyze.return_value = result()
    await history.analyze(service, "u", "c", "第一轮", [])
    service.analyze.side_effect = ToolError("等待回复超时")
    with pytest.raises(ToolError, match="query_analysis_result"):
        await history.analyze(service, "u", "c", "第二轮", [])
    page = history.query("u", "c", limit=1)
    assert page["status"] == "unknown"
    assert page["records"][0]["result"] is None
    older = history.query("u", "c", before_id=page["next_before_id"])
    assert older["records"][0]["query"] == "第一轮"
    assert older["records"][0]["result"]["content"] == "答案"
    await history.close()


async def test_shutdown_and_crash_mark_pending_as_unknown(tmp_path):
    path = str(tmp_path / "history.sqlite3")
    history = AnalysisHistory(path)
    started = asyncio.Event()

    async def analyze(*args):
        started.set()
        await asyncio.Event().wait()

    service = AsyncMock()
    service.analyze.side_effect = analyze
    caller = asyncio.create_task(history.analyze(service, "u", "c", "问题", []))
    await started.wait()
    await history.close()
    with pytest.raises(asyncio.CancelledError):
        await caller
    history = AnalysisHistory(path)
    assert history.query("u", "c")["status"] == "unknown"
    # Simulate a record left running by a hard process exit.
    history.db.execute("UPDATE analyses SET status='running'")
    history.db.commit()
    await history.close()
    history = AnalysisHistory(path)
    assert history.query("u", "c")["status"] == "unknown"
    await history.close()
