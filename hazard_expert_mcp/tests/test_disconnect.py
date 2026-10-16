import asyncio
import socket
import threading
import time
from unittest.mock import AsyncMock

import httpx
import uvicorn

from hazard_expert_mcp.app import create_app
from hazard_expert_mcp.service import AnalysisResult


def test_http_timeout_then_query_saved_answer(settings):
    started, release = threading.Event(), threading.Event()

    async def analyze(*args):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return AnalysisResult(status="completed", user_id="u", chat_id="c", content="最终结果", attachments=[])

    service = AsyncMock()
    service.analyze.side_effect = analyze
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(create_app(settings, service), log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    headers = {"Authorization": "Bearer test-service-token", "Accept": "application/json, text/event-stream"}
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", headers=headers, trust_env=False) as client:
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "hazard_analysis_expert", "arguments": {"user_id": "u", "chat_id": "c", "query": "分析"},
            }}
            try:
                client.post("/mcp", json=request, timeout=0.3)
                raise AssertionError("The client should time out while analysis is running")
            except httpx.ReadTimeout:
                pass
            assert started.wait(2)
            request["id"] = 2
            request["params"] = {"name": "query_analysis_result", "arguments": {"user_id": "u", "chat_id": "c"}}

            def query():
                response = client.post("/mcp", json=request)
                response.raise_for_status()
                return response.json()["result"]["structuredContent"]

            assert query()["status"] == "running"
            release.set()
            deadline = time.monotonic() + 5
            record = query()
            while record["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.02)
                record = query()
            assert record["status"] == "completed"
            assert record["records"][0]["result"]["content"] == "最终结果"
            service.analyze.assert_awaited_once()
    finally:
        release.set()
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
