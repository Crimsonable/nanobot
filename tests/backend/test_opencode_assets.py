"""Mounted executable and plugin asset checks for the OpenCode facade."""
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_backend.config import RuntimeConfig
from bucket_runtime.opencode_server import OpenCodeServer


@pytest.mark.asyncio
async def test_missing_mounted_binary_fails_before_launch(tmp_path):
    config = RuntimeConfig(frontend_id="web", common_root=tmp_path / "common", bucket_id="b", state_root=tmp_path / "state",
        workspace_root=tmp_path / "users", opencode={"binary": tmp_path / "missing", "context_root": tmp_path / "context"})
    opencode = OpenCodeServer(config)
    try:
        with pytest.raises(RuntimeError, match="runtime file missing"):
            await opencode._start_process()
    finally:
        await opencode.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("final", [{"status": "completed"}, {"status": "error", "error": "unreadable history"}])
async def test_waits_for_native_storage_upgrade_and_surfaces_failure(monkeypatch, final):
    client = OpenCodeServer(RuntimeConfig(frontend_id="web", bucket_id="b"))
    await client.http.aclose()
    states = iter([{"status": "running", "progress": {"label": "Migrating sessions"}}, final])
    def handle(request):
        assert request.url.path == "/api/experimental/migration/v1"
        return httpx.Response(200, json=next(states))
    pause = AsyncMock()
    monkeypatch.setattr("bucket_runtime.opencode_server.asyncio.sleep", pause)
    async with httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(handle)) as client.http:
        if final["status"] == "error":
            with pytest.raises(RuntimeError, match="unreadable history"):
                await client._wait_for_storage()
        else:
            await client._wait_for_storage()
    pause.assert_awaited_once_with(0.5)
