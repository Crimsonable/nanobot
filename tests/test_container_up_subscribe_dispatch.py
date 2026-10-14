import asyncio
from unittest.mock import AsyncMock

import pytest

from container_up.server import ControlService


@pytest.mark.asyncio
async def test_subscribe_failure_is_logged_with_context(caplog):
    service = object.__new__(ControlService)
    service.submit = AsyncMock(side_effect=RuntimeError("bucket not ready"))
    with pytest.raises(RuntimeError, match="bucket not ready"):
        await service.dispatch_channel("qxt-main", {"event": {
            "user_id": "alice", "chat_id": "chat-1"}})
    assert "subscribe dispatch failed frontend_id=qxt-main chat_id=chat-1" in caplog.text


@pytest.mark.asyncio
async def test_subscribe_cancellation_propagates(caplog):
    service = object.__new__(ControlService)
    service.submit = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await service.dispatch_channel("qxt-main", {"event": {"user_id": "alice", "chat_id": "chat"}})
    assert "subscribe dispatch failed" not in caplog.text
