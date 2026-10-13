from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from container_up import app as app_module


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prepare", "dispatch"])
async def test_subscribe_failure_is_logged_with_context(monkeypatch, stage):
    payload = {"event": {"chat_id": "chat-1", "metadata": {"frontend_id": "qxt-main"}}}
    prepare = AsyncMock(return_value=payload)
    dispatch = AsyncMock()
    failing_call = prepare if stage == "prepare" else dispatch
    failing_call.side_effect = RuntimeError("instance not ready")
    log = Mock()
    monkeypatch.setattr(app_module, "get_im_parser", lambda _: SimpleNamespace(prepare_inbound_event=prepare))
    monkeypatch.setattr(app_module, "_dispatch_im_event", dispatch)
    monkeypatch.setattr(app_module.logger, "exception", log)

    await app_module._dispatch_subscribe_event(payload)

    log.assert_called_once_with(
        "subscribe dispatch failed frontend_id={} chat_id={}", "qxt-main", "chat-1",
    )
    if stage == "prepare":
        dispatch.assert_not_awaited()
    else:
        dispatch.assert_awaited_once_with(payload)


@pytest.mark.asyncio
async def test_subscribe_cancellation_propagates(monkeypatch):
    monkeypatch.setattr(app_module, "get_im_parser", lambda _: SimpleNamespace())
    monkeypatch.setattr(app_module, "_dispatch_im_event", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await app_module._dispatch_subscribe_event({"event": {}})
