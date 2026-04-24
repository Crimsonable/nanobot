import asyncio

import pytest

pytest.importorskip("docker")

import container_up.app as app_module


@pytest.mark.asyncio
async def test_bridge_ws_outbound_dispatch_failure_is_returned_not_raised(monkeypatch) -> None:
    async def _raise(_event):
        raise RuntimeError("send failed")

    monkeypatch.setattr(app_module.dispatch_parser, "parse", _raise)

    result = await app_module._dispatch_bridge_outbound_from_ws(
        "org-1",
        {
            "chat_id": "chat-1",
            "content": "done",
            "metadata": {},
            "attachments": ["/tmp/report.pdf"],
        },
    )

    assert result == {"ok": False, "error": "send failed"}


@pytest.mark.asyncio
async def test_bridge_ws_outbound_dispatch_preserves_cancellation(monkeypatch) -> None:
    async def _cancel(_event):
        raise asyncio.CancelledError

    monkeypatch.setattr(app_module.dispatch_parser, "parse", _cancel)

    with pytest.raises(asyncio.CancelledError):
        await app_module._dispatch_bridge_outbound_from_ws(
            "org-1",
            {
                "chat_id": "chat-1",
                "content": "done",
                "metadata": {},
                "attachments": [],
            },
        )
