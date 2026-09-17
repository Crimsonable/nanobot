from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from container_up import app as app_module


@pytest.mark.asyncio
async def test_sync_inbound_returns_final_reply_without_using_existing_outbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routed = asyncio.Event()
    inbound_packet: dict[str, Any] = {}
    forwarded: list[dict[str, Any]] = []

    async def fake_route(**kwargs: Any) -> dict[str, str]:
        inbound_packet.update(kwargs)
        routed.set()
        return {"bucket_id": "bucket-1", "instance_id": "instance-1"}

    async def fake_forward(packet: dict[str, Any]) -> dict[str, Any]:
        forwarded.append(packet)
        return {"ok": True}

    async def fake_attachments(attachments: list[Any], **kwargs: Any) -> list[Any]:
        assert kwargs["frontend_id"] == "web-wd"
        assert kwargs["user_id"] == "user-1"
        return [{"url": "https://files.example/report.pdf"}]

    monkeypatch.setattr(app_module, "_route_message", fake_route)
    monkeypatch.setattr(app_module, "_forward_outbound_message", fake_forward)
    monkeypatch.setattr("container_up.sync_reply.frontend_config_for", lambda _: None)
    monkeypatch.setattr("container_up.sync_reply.prepare_outbound_attachments", fake_attachments)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://test"
    ) as client:
        pending_request = asyncio.create_task(
            client.post(
                "/inbound-sync/web-wd",
                json={
                    "user_id": "user-1",
                    "chat_id": "chat-1",
                    "content": "hello",
                    "metadata": {"trace_id": "trace-1"},
                },
            )
        )
        await asyncio.wait_for(routed.wait(), timeout=1)
        metadata = inbound_packet["metadata"]
        assert metadata["trace_id"] == "trace-1"
        request_id = metadata["_sync_request_id"]

        progress = await client.post(
            "/outbound",
            json={
                "frontend_id": "web-wd",
                "user_id": "user-1",
                "chat_id": "chat-1",
                "content": "working",
                "metadata": {"_sync_request_id": request_id, "_progress": True},
            },
        )
        assert progress.status_code == 200
        assert not pending_request.done()

        reply = await client.post(
            "/outbound",
            json={
                "frontend_id": "web-wd",
                "user_id": "user-1",
                "chat_id": "chat-1",
                "content": "done",
                "attachments": ["/tmp/report.pdf"],
                "metadata": {"_sync_request_id": request_id, "trace_id": "trace-1"},
            },
        )
        result = await asyncio.wait_for(pending_request, timeout=1)
        assert reply.status_code == 200
        assert result.status_code == 200
        assert result.json() == {
            "status": "completed",
            "frontend_id": "web-wd",
            "user_id": "user-1",
            "chat_id": "chat-1",
            "bucket_id": "bucket-1",
            "instance_id": "instance-1",
            "content": "done",
            "attachments": [{"url": "https://files.example/report.pdf"}],
            "metadata": {"trace_id": "trace-1"},
        }
        assert forwarded == []

        ordinary = await client.post(
            "/outbound",
            json={
                "frontend_id": "web-wd",
                "user_id": "user-1",
                "chat_id": "chat-1",
                "content": "ordinary",
            },
        )
        assert ordinary.status_code == 200
        assert forwarded[0]["content"] == "ordinary"


@pytest.mark.asyncio
async def test_sync_inbound_times_out_and_drops_late_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    forwarded: list[dict[str, Any]] = []

    async def fake_route(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"bucket_id": "bucket-1", "instance_id": "instance-1"}

    async def fake_forward(packet: dict[str, Any]) -> dict[str, Any]:
        forwarded.append(packet)
        return {"ok": True}

    monkeypatch.setattr(app_module, "_route_message", fake_route)
    monkeypatch.setattr(app_module, "_forward_outbound_message", fake_forward)
    monkeypatch.setattr(app_module, "SYNC_REPLY_TIMEOUT_SECONDS", 0.01)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/inbound-sync/web-wd",
            json={"user_id": "user-1", "chat_id": "chat-1", "content": "hello"},
        )
        assert response.status_code == 504
        late = await client.post(
            "/outbound",
            json={
                "frontend_id": "web-wd",
                "user_id": "user-1",
                "chat_id": "chat-1",
                "content": "late",
                "metadata": captured["metadata"],
            },
        )
        assert late.status_code == 200
        assert forwarded == []
