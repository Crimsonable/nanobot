from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bucket_runtime.process_manager import ProcessManager, UserProcess


class _ReadySocket:
    def __init__(self, *, gateway_ready: bool) -> None:
        self.gateway_ready = gateway_ready
        self.sent: list[dict[str, object]] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        return json.dumps(
            {
                "type": "ready_status",
                "gateway_ready": self.gateway_ready,
            }
        )

    async def __aenter__(self) -> "_ReadySocket":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None


@pytest.mark.asyncio
async def test_wait_instance_ready_requires_gateway_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ProcessManager(idle_ttl=60)
    attempts = [False, True]
    sockets: list[_ReadySocket] = []

    def fake_connect(*_args, **_kwargs) -> _ReadySocket:
        socket = _ReadySocket(gateway_ready=attempts.pop(0))
        sockets.append(socket)
        return socket

    monkeypatch.setattr("bucket_runtime.process_manager.websockets.connect", fake_connect)

    await manager._wait_instance_ready(20123)

    assert len(sockets) == 2
    assert [socket.sent for socket in sockets] == [[{"type": "ready_check"}], [{"type": "ready_check"}]]


@pytest.mark.asyncio
async def test_forward_outbound_preserves_chat_id_and_identity_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ProcessManager(idle_ttl=60)
    captured: dict[str, object] = {}

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

    monkeypatch.setattr("bucket_runtime.process_manager.httpx.AsyncClient", _FakeAsyncClient)

    instance = UserProcess(
        instance_id="inst-1",
        frontend_id="feishu-main",
        user_id="user-1",
        workspace_path=SimpleNamespace(),
        port=20123,
        process=SimpleNamespace(returncode=None, stdout=None),
        started_at=0.0,
        last_active_at=0.0,
    )

    await manager._forward_outbound(
        instance,
        {
            "chat_id": "conv-1",
            "content": "done",
            "attachments": ["/tmp/report.png"],
            "metadata": {"trace_id": "trace-1"},
        },
    )

    assert captured["json"] == {
        "frontend_id": "feishu-main",
        "user_id": "user-1",
        "chat_id": "conv-1",
        "content": "done",
        "attachments": ["/tmp/report.png"],
        "metadata": {
            "trace_id": "trace-1",
            "frontend_id": "feishu-main",
            "usr_id": "user-1",
        },
        "raw": {"source": "bucket-runtime", "instance_id": "inst-1"},
    }


@pytest.mark.asyncio
async def test_relay_instance_keeps_running_when_outbound_forward_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ProcessManager(idle_ttl=60)
    forwarded: list[dict[str, object]] = []

    async def fake_forward(_instance: UserProcess, packet: dict[str, object]) -> None:
        forwarded.append(packet)
        raise RuntimeError("delivery failed")

    monkeypatch.setattr(manager, "_forward_outbound", fake_forward)

    class _Socket:
        def __init__(self) -> None:
            self._done = False

        def __aiter__(self) -> "_Socket":
            return self

        async def __anext__(self) -> str:
            if self._done:
                raise StopAsyncIteration
            self._done = True
            return json.dumps({"type": "outbound_message", "chat_id": "conv-1"})

    websocket = _Socket()
    instance = UserProcess(
        instance_id="inst-1",
        frontend_id="feishu-main",
        user_id="user-1",
        workspace_path=SimpleNamespace(),
        port=20123,
        process=SimpleNamespace(returncode=None, stdout=None),
        started_at=0.0,
        last_active_at=0.0,
        websocket=websocket,
    )

    await manager._relay_instance(instance, websocket)

    assert forwarded == [{"type": "outbound_message", "chat_id": "conv-1"}]
    assert instance.websocket is None


def test_resolve_idle_ttl_uses_frontend_override() -> None:
    manager = ProcessManager(idle_ttl=60)
    frontend_config = SimpleNamespace(raw={"instance_idle_ttl_seconds": 120})
    assert manager._resolve_idle_ttl(frontend_config) == 120


@pytest.mark.asyncio
async def test_reap_idle_processes_uses_instance_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ProcessManager(idle_ttl=100)
    now = 1_000.0
    monkeypatch.setattr("bucket_runtime.process_manager.time.time", lambda: now)
    stopped: list[str] = []

    async def fake_stop_process(
        instance_id: str,
        *,
        notify_release: bool = False,
        reason: str = "",
    ) -> None:
        stopped.append(instance_id)
        manager._processes.pop(instance_id, None)

    monkeypatch.setattr(manager, "stop_process", fake_stop_process)

    manager._processes["a"] = UserProcess(
        instance_id="a",
        frontend_id="feishu-main",
        user_id="u1",
        workspace_path=SimpleNamespace(),
        port=20001,
        process=SimpleNamespace(returncode=None, stdout=None),
        started_at=900.0,
        last_active_at=950.0,
        idle_ttl_seconds=30,
    )
    manager._processes["b"] = UserProcess(
        instance_id="b",
        frontend_id="web-main",
        user_id="u2",
        workspace_path=SimpleNamespace(),
        port=20002,
        process=SimpleNamespace(returncode=None, stdout=None),
        started_at=900.0,
        last_active_at=950.0,
        idle_ttl_seconds=120,
    )

    await manager.reap_idle_processes()
    assert stopped == ["a"]
