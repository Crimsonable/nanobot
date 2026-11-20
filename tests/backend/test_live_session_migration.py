"""Exercise native transcript APIs and cross-bucket continuation on real servers."""
import asyncio
import json
import os
import socket
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
import pytest

from agent_backend.models import Identity, Submit
from agent_backend.config import RuntimeConfig
from bucket_runtime.opencode_server import OpenCodeServer



@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("OPENCODE_TEST_BINARY"),
                    reason="requires explicit pinned OpenCode 2.x binary")
async def test_native_servers_read_complete_history_after_round_trip(tmp_path):
    identity = Identity(frontend_id="web", user_id="alice")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    processes, clients = [], []
    try:
        for name in ["a", "b"]:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            home = tmp_path / name
            env = {key: os.environ[key] for key in ["PATH", "TMPDIR", "SYSTEMROOT", "TEMP", "TMP"] if key in os.environ}
            env.update({"HOME": str(home), "USERPROFILE": str(home), "XDG_DATA_HOME": str(home / "data"),
                "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache"),
                "XDG_STATE_HOME": str(home / "state"), "OPENCODE_PASSWORD":"migration-test",
                "OPENCODE_DISABLE_MODELS_FETCH": "true",
                "OPENCODE_CONFIG_CONTENT": json.dumps({"update": "disable", "snapshots": False, "plugins": []})})
            processes.append(await asyncio.create_subprocess_exec(os.environ["OPENCODE_TEST_BINARY"],
                "serve", "--hostname", "127.0.0.1", "--port", str(port), env=env, cwd=workspace,
                stdout=asyncio.subprocess.DEVNULL))
            # Match the runtime transport budget for cold location initialization.
            # Inherit stderr so native diagnostics cannot fill an undrained pipe.
            client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                                       timeout=30, trust_env=False, auth=("opencode","migration-test"))
            clients.append(client)
            for _ in range(200):
                if processes[-1].returncode is not None:
                    pytest.fail(f"OpenCode exited with {processes[-1].returncode}; see captured stderr")
                try:
                    response = await client.get("/api/info")
                    response.raise_for_status()
                    break
                except (httpx.TransportError, httpx.HTTPStatusError):
                    await asyncio.sleep(0.1)
            else:
                pytest.fail("OpenCode did not become healthy")
            response = await client.get("/api/location", params={"location[directory]":str(workspace)})
            response.raise_for_status()
        context = {**identity.model_dump(), "identity_id": identity.identity_id,
                   "chat_id": "chat", "schema_version": 1, "bucket_id": "a", "requests": {}}
        response = await clients[0].post("/api/session", json={"location":{"directory":str(workspace)}, "title": "Migration", "permissions": [],
                                            "metadata": {"bucket_runtime": context}})
        response.raise_for_status()
        info = response.json()["data"]
        session = info["id"]
        # Seed a settled native transcript through the public import API.
        (await clients[0].delete(f"/api/session/{session}")).raise_for_status()
        messages = [{"id": "msg_history", "type": "assistant", "agent": "build",
            "model": {"providerID": "test", "id": "test"},
            "content": [{"type": "text", "text": "Preserve this native history"},
                {"type": "tool", "id": "call_history", "name": "read", "executed": True,
                 "state": {"status": "completed", "input": {"path": "notes.txt"},
                           "content": [{"type": "text", "text": "kept tool result"}]},
                 "time": {"created": 1, "ran": 1, "completed": 2}}],
            "time": {"created": 1, "completed": 2}}]
        response = await clients[0].post("/api/experimental/session/import",
            json={"info": info, "messages": messages, "location": {"directory": str(workspace)}})
        assert response.is_success, response.text
        for source, target in [(0, 1), (1, 0)]:
            response = await clients[source].get(f"/api/experimental/session/{session}/export")
            response.raise_for_status()
            history = response.json()["data"]
            assert history["messages"][0]["content"][1]["state"]["content"][0]["text"] == "kept tool result"
            response = await clients[target].post("/api/experimental/session/import",
                json={**history, "location": {"directory": str(workspace)}})
            assert response.is_success, response.text
            copied = (await clients[target].get(f"/api/experimental/session/{session}/export")).json()["data"]
            assert copied["info"]["id"] == session
            assert copied["messages"] == history["messages"]
            (await clients[source].delete(f"/api/session/{session}")).raise_for_status()
            assert (await clients[source].get(f"/api/session/{session}")).status_code == 404
        # A parked inbox item is unfinished work, even when no execution is active.
        native = OpenCodeServer(RuntimeConfig(frontend_id="web", bucket_id="a",
            state_root=tmp_path / "runtime", workspace_root=workspace))
        await native.http.aclose()
        native.http = clients[0]
        assert await native.sessions_idle(await native.session_tree(session))
        response = await clients[0].post(f"/api/session/{session}/prompt", json={
            "id": "msg_pending", "text": "not completed", "resume": False})
        response.raise_for_status()
        assert not await native.sessions_idle(await native.session_tree(session))
        exported = (await clients[0].get(f"/api/experimental/session/{session}/export")).json()["data"]
        assert exported["messages"] == copied["messages"]
    finally:
        for client in clients:
            await client.aclose()
        for process in processes:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("OPENCODE_TEST_BINARY"),
                    reason="requires explicit pinned OpenCode 2.x binary and built plugin")
async def test_capacity_reassignment_continues_history_through_real_runtimes(tmp_path):
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
    from support import register_bucket
    from test_live_opencode import Channels, port, running
    from websockets.asyncio.client import connect

    from agent_backend.config import ControlConfig, GatewaySettings, RuntimeConfig
    from agent_backend.wire import Peer
    from bucket_runtime.adapter import BucketRuntimeAdapter
    from bucket_runtime.opencode_server import OpenCodeServer
    from bucket_runtime.sandbox_executor import SandboxExecutor
    from bucket_runtime.server import rpc_app
    from container_up.server import create_app

    model_port, cu_port = port(), port()
    calls, runtimes, servers, tasks = [], [], [], []
    unblock = asyncio.Event()
    blocked = asyncio.Event()
    model = FastAPI()

    @model.post("/v1/chat/completions")
    async def completion(request: Request):
        body = await request.json()
        calls.append(body)
        if "OCCUPY_BUCKET" in str(body["messages"][-1]):
            blocked.set()
            await unblock.wait()

        async def stream():
            yield "data: " + json.dumps({"id": "test", "object": "chat.completion.chunk", "created": 1,
                "model": "test", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Memory retained."},
                                              "finish_reason": None}]}) + "\n\n"
            yield "data: " + json.dumps({"id": "test", "object": "chat.completion.chunk", "created": 1,
                "model": "test", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    common = tmp_path / "common"
    (common / "web/app").mkdir(parents=True)
    (common / "web/config").mkdir()
    config_path = common / "web/config/opencode.json"
    config_path.write_text(json.dumps({"update": "disable", "snapshots": False, "providers": {"test": {
        "package": "@opencode/ai/providers/openai-compatible", "name": "Local test",
        "settings": {"baseURL": f"http://127.0.0.1:{model_port}/v1", "apiKey": "unused"},
        "models": {"test": {"name": "test", "limit": {"context": 32000, "output": 4096}}}}},
        "model": "test/test"}))
    control = ControlConfig(state_root=tmp_path / "control", frontends={"web": {"provider": "web"}},
                            bucket_pool={"max_users_per_bucket": 1}, limits={"request_seconds": 60})
    app = create_app(control)
    from channel_gateway.server import create_app as gateway_app
    gateway_port = port()
    gateway = gateway_app(control, GatewaySettings(state_root=tmp_path / "gateway",
        control_url=f"http://127.0.0.1:{cu_port}"), Channels())
    plugin = Path(__file__).resolve().parents[2] / "opencode_plugin/dist/index.mjs"
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(running(model, model_port))
        await stack.enter_async_context(running(app, cu_port))
        await stack.enter_async_context(running(gateway, gateway_port))
        client = await stack.enter_async_context(httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway_port}",
                                                                  timeout=70, trust_env=False))

        async def start_bucket(bucket_id):
            config = RuntimeConfig(frontend_id="web", bucket_id=bucket_id, common_root=common,
                state_root=tmp_path / "runtime/web" / bucket_id, workspace_root=tmp_path / "users",
                control_url=f"ws://127.0.0.1:{cu_port}/internal/buckets/ws", rpc_port=port(), health_port=port(),
                opencode={"binary": os.environ["OPENCODE_TEST_BINARY"], "port": port(),
                    "context_root": tmp_path / "context", "config_file": config_path,
                    "plugin_dir": plugin.parent.parent})
            opencode = OpenCodeServer(config)
            servers.append(opencode)
            runtime = BucketRuntimeAdapter(config, opencode, SandboxExecutor(config.sandbox, config.limits.output_bytes))
            runtimes.append(runtime)
            await stack.enter_async_context(running(rpc_app(runtime, opencode.rpc_token), config.rpc_port))
            await opencode.start(runtime.on_opencode_event)
            assert runtime.opencode.plugin_ready.is_set()
            register_bucket(app.state.service, bucket_id)
            template = control.runtime_templates["web"]
            template.state_root, template.workspace_root = tmp_path / "runtime", tmp_path / "users"
            socket_ = await stack.enter_async_context(connect(config.control_url, proxy=None,
                                                              additional_headers={"X-Bucket-ID": bucket_id}))
            runtime.peer = Peer(socket_, runtime.db, bucket_id, runtime.on_command, runtime.control)
            tasks.extend([asyncio.create_task(runtime.peer.run()), asyncio.create_task(runtime.process_events()),
                          asyncio.create_task(opencode.watch())])
            gw_socket = await stack.enter_async_context(connect(f"ws://127.0.0.1:{gateway_port}/internal/events/ws", proxy=None,
                                                               additional_headers={"X-Bucket-ID": bucket_id}))
            async def reject(event):
                raise AssertionError("unexpected gateway command")
            tasks.append(asyncio.create_task(Peer(gw_socket, runtime.db, bucket_id, reject,
                journal_peer="gateway", protocol="agent-gateway/1").run()))
            return runtime

        try:
            source = await start_bucket("a")
            first = await client.post("/inbound-sync/web", json={"user_id": "alice", "chat_id": "chat",
                "content": "ORIGINAL_MEMORY_721", "request_id": "first"})
            assert first.status_code == 200 and first.json()["status"] == "completed", first.text
            session_id = source.db.get("run", "first")["session_id"]
            occupied = await client.post("/inbound/web", json={"user_id": "bob", "chat_id": "chat",
                "content": "OCCUPY_BUCKET", "request_id": "busy"})
            assert occupied.status_code == 200
            await asyncio.wait_for(blocked.wait(), 20)
            target = await start_bucket("b")
            moved = await client.post("/inbound-sync/web", json={"user_id": "alice", "chat_id": "chat",
                "content": "Continue our previous conversation", "request_id": "second"})
            assert moved.status_code == 200 and moved.json()["status"] == "completed", moved.text
            assert moved.json()["bucket_id"] == "b"
            assert target.db.get("run", "second")["session_id"] == session_id
            assert "ORIGINAL_MEMORY_721" in str(calls[-1]["messages"])
            assert "Memory retained." in str(calls[-1]["messages"])
            response = await source.opencode.http.get(
                f"/api/session/{session_id}")
            assert response.status_code == 404
            key = Submit.model_validate(source.db.get("run", "first")["submit"]).session_key
            assert source.db.get("session", key) is None
            assert app.state.service.db.get("session", key)["bucket_id"] == "b"
        finally:
            unblock.set()
            for runtime in runtimes:
                await runtime.shutdown()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for opencode in servers:
                await opencode.close()
            for runtime in runtimes:
                runtime.db.close()
