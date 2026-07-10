from __future__ import annotations

import json
import sys
from pathlib import Path
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


async def _completed_coro() -> None:
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
async def test_forward_inbound_restarts_instance_when_workspace_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = ProcessManager(idle_ttl=60)
    workspace = tmp_path / "workspaces" / "web-main" / "user-1"
    sent_packets: list[dict[str, object]] = []
    stopped_instances: list[str] = []

    async def fake_send_instance(_instance: UserProcess, packet: dict[str, object]) -> None:
        sent_packets.append(packet)

    async def fake_stop_instance(
        instance: UserProcess,
        *,
        notify_release: bool = False,
        reason: str = "",
    ) -> None:
        stopped_instances.append(instance.instance_id)

    monkeypatch.setattr(manager, "_send_instance", fake_send_instance)
    monkeypatch.setattr(manager, "_stop_instance", fake_stop_instance)
    monkeypatch.setattr(manager, "_wait_instance_ready", lambda _port: _completed_coro())
    monkeypatch.setattr(manager, "_ensure_instance_socket", lambda _instance: _completed_coro())

    frontend_config = SimpleNamespace(
        config_path=tmp_path / "common" / "web-main" / "config.json",
        template_dir=tmp_path / "templates",
        builtin_skills_dir=tmp_path / "skills",
        raw={},
    )
    monkeypatch.setattr(
        "bucket_runtime.process_manager.frontend_config_for",
        lambda _frontend_id: frontend_config,
    )
    monkeypatch.setattr(
        manager._workspace_manager,
        "ensure_workspace",
        lambda workspace_path, *, template_root: workspace_path,
    )

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return SimpleNamespace(returncode=None, stdout=None)

    monkeypatch.setattr(
        "bucket_runtime.process_manager.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    instance = UserProcess(
        instance_id="inst-1",
        frontend_id="web-main",
        user_id="user-1",
        workspace_path=workspace,
        port=20123,
        process=SimpleNamespace(returncode=None, stdout=None),
        started_at=0.0,
        last_active_at=0.0,
    )
    manager._processes["inst-1"] = instance

    await manager.forward_inbound(
        "inst-1",
        {
            "channel": "bridge",
            "chat_id": "conv-1",
            "content": "hello",
            "attachments": [],
            "metadata": {"trace_id": "trace-1"},
        },
    )

    assert stopped_instances == ["inst-1"]
    assert manager._processes["inst-1"] is not instance
    assert sent_packets == [
        {
            "type": "inbound_message",
            "channel": "bridge",
            "chat_id": "conv-1",
            "content": "hello",
            "attachments": [],
            "metadata": {"trace_id": "trace-1"},
        }
    ]


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


def test_build_process_env_exports_container_up_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ProcessManager(idle_ttl=60)
    monkeypatch.setattr(
        "bucket_runtime.process_manager.CONTAINER_UP_ATTACHMENT_UPLOAD_URL",
        "http://container-up:8080/internal/attachments/upload",
    )
    monkeypatch.setattr(
        "bucket_runtime.process_manager.CONTAINER_UP_BRIDGE_OUTBOUND_URL",
        "http://container-up:8080/api/bridge/outbound",
    )

    frontend_config = SimpleNamespace(
        template_dir="/tmp/template",
        builtin_skills_dir="/tmp/skills",
    )
    env = manager._build_process_env(frontend_config)

    assert (
        env["CONTAINER_UP_ATTACHMENT_UPLOAD_URL"]
        == "http://container-up:8080/internal/attachments/upload"
    )
    assert (
        env["CONTAINER_UP_BRIDGE_OUTBOUND_URL"]
        == "http://container-up:8080/api/bridge/outbound"
    )


@pytest.mark.asyncio
async def test_create_instance_passes_workspace_and_config_to_local_service_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = ProcessManager(idle_ttl=60)
    workspace = tmp_path / "workspaces" / "web-wd" / "web-demo-2"
    config_path = tmp_path / "common" / "web-wd" / "config.json"
    template_dir = tmp_path / "common" / "web-wd" / "templates"
    skills_dir = tmp_path / "common" / "web-wd" / "skills"
    captured: dict[str, object] = {}

    frontend_config = SimpleNamespace(
        config_path=config_path,
        template_dir=template_dir,
        builtin_skills_dir=skills_dir,
        raw={},
    )

    monkeypatch.setattr(
        "bucket_runtime.process_manager.frontend_config_for",
        lambda frontend_id: frontend_config,
    )
    monkeypatch.setattr(
        manager._workspace_manager,
        "ensure_workspace",
        lambda workspace_path, *, template_root: workspace_path,
    )
    monkeypatch.setattr(manager, "_wait_instance_ready", lambda _port: _completed_coro())
    monkeypatch.setattr(manager, "_ensure_instance_socket", lambda _instance: _completed_coro())

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=None, stdout=None)

    monkeypatch.setattr(
        "bucket_runtime.process_manager.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    instance = await manager.create_instance(
        frontend_id="web-wd",
        user_id="web-demo-2",
        instance_id="web-wd__web-demo-2",
        workspace_path=str(workspace),
    )

    args = list(captured["args"])
    assert args[:4] == [sys.executable, "-m", "bucket_runtime.local_service", "--config"]
    assert str(args[4]) == str(config_path)
    assert args[5:7] == ["--workspace", str(workspace)]
    assert instance.workspace_path == workspace
    assert instance.instance_id == "web-wd__web-demo-2"
    assert instance.frontend_id == "web-wd"
    assert instance.user_id == "web-demo-2"


@pytest.mark.asyncio
async def test_create_instance_restarts_live_process_when_workspace_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = ProcessManager(idle_ttl=60)
    workspace = tmp_path / "workspaces" / "web-wd" / "web-demo-2"
    stopped_instances: list[str] = []
    frontend_config = SimpleNamespace(
        config_path=tmp_path / "common" / "web-wd" / "config.json",
        template_dir=tmp_path / "common" / "web-wd" / "templates",
        builtin_skills_dir=tmp_path / "common" / "web-wd" / "skills",
        raw={},
    )

    monkeypatch.setattr(
        "bucket_runtime.process_manager.frontend_config_for",
        lambda _frontend_id: frontend_config,
    )
    monkeypatch.setattr(
        manager._workspace_manager,
        "ensure_workspace",
        lambda workspace_path, *, template_root: workspace_path,
    )
    monkeypatch.setattr(manager, "_wait_instance_ready", lambda _port: _completed_coro())
    monkeypatch.setattr(manager, "_ensure_instance_socket", lambda _instance: _completed_coro())

    async def fake_stop_instance(
        instance: UserProcess,
        *,
        notify_release: bool = False,
        reason: str = "",
    ) -> None:
        stopped_instances.append(instance.instance_id)

    monkeypatch.setattr(manager, "_stop_instance", fake_stop_instance)

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return SimpleNamespace(returncode=None, stdout=None)

    monkeypatch.setattr(
        "bucket_runtime.process_manager.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    old_instance = UserProcess(
        instance_id="web-wd__web-demo-2",
        frontend_id="web-wd",
        user_id="web-demo-2",
        workspace_path=workspace,
        port=20123,
        process=SimpleNamespace(returncode=None, stdout=None),
        started_at=0.0,
        last_active_at=0.0,
    )
    manager._processes[old_instance.instance_id] = old_instance

    new_instance = await manager.create_instance(
        frontend_id="web-wd",
        user_id="web-demo-2",
        instance_id="web-wd__web-demo-2",
        workspace_path=str(workspace),
    )

    assert stopped_instances == ["web-wd__web-demo-2"]
    assert new_instance is manager._processes["web-wd__web-demo-2"]
    assert new_instance is not old_instance
    assert new_instance.workspace_path == workspace


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
