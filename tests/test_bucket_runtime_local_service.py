from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bucket_runtime.local_service import LocalNanobotService


class _FakeRouterWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def __aiter__(self) -> "_FakeRouterWebSocket":
        return self

    async def __anext__(self) -> str:
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_router_ready_check_reports_gateway_state(tmp_path: Path) -> None:
    service = LocalNanobotService(
        config_path=tmp_path / "config.json",
        workspace_path=tmp_path / "workspace",
        host="127.0.0.1",
        port=29995,
    )
    service._gateway_ready.set()
    websocket = _FakeRouterWebSocket()

    await service._run_router_session(websocket, {"type": "ready_check"})

    assert websocket.sent == [{"type": "ready_status", "gateway_ready": True}]


@pytest.mark.asyncio
async def test_spawn_gateway_uses_dedicated_health_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    service = LocalNanobotService(
        config_path=tmp_path / "config.json",
        workspace_path=tmp_path / "workspace",
        host="127.0.0.1",
        port=29995,
    )
    captured: dict[str, object] = {}

    async def fake_watch() -> None:
        await asyncio.sleep(3600)

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=None)

    monkeypatch.setattr(service, "_allocate_gateway_port", lambda: 31111)
    monkeypatch.setattr(service, "_watch_gateway_process", fake_watch)
    monkeypatch.setattr(
        "bucket_runtime.local_service.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    await service._spawn_gateway()

    args = list(captured["args"])
    port_index = args.index("--port")
    assert args[port_index + 1] == "31111"
    assert args[port_index + 1] != "29995"
    env = captured["kwargs"]["env"]
    assert env["BRIDGE_URL_OVERRIDE"] == "ws://127.0.0.1:29995"
    assert env["BRIDGE_ALLOW_FROM_OVERRIDE"] == "*"

    assert service._gateway_watch_task is not None
    service._gateway_watch_task.cancel()
    await asyncio.gather(service._gateway_watch_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_send_router_falls_back_to_direct_outbound_when_router_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = LocalNanobotService(
        config_path=tmp_path / "config.json",
        workspace_path=tmp_path / "workspace",
        host="127.0.0.1",
        port=29995,
    )
    captured: dict[str, object] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url: str, json: dict[str, object]) -> _FakeResponse:
            captured["url"] = url
            captured["json"] = json
            return _FakeResponse()

    monkeypatch.setattr("bucket_runtime.local_service.httpx.AsyncClient", _FakeAsyncClient)

    await service._send_router(
        {
            "type": "outbound_message",
            "chat_id": "conv-1",
            "content": "done",
            "attachments": ["/tmp/a.png"],
            "metadata": {
                "frontend_id": "feishu-main",
                "usr_id": "user-1",
                "trace_id": "trace-1",
            },
        }
    )

    assert captured["json"] == {
        "frontend_id": "feishu-main",
        "user_id": "user-1",
        "chat_id": "conv-1",
        "content": "done",
        "attachments": ["/tmp/a.png"],
        "metadata": {
            "frontend_id": "feishu-main",
            "usr_id": "user-1",
            "trace_id": "trace-1",
        },
        "raw": {"source": "bucket-runtime-local-service"},
    }
