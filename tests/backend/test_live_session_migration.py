"""Exercise native SQLite moves against two real pinned servers; no model calls."""
import asyncio
import json
import os
import socket
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
import pytest

from agent_backend.models import Identity, Submit
from agent_backend.session_history import (
    database, delete_history, export_history, import_history, validate_destination,
)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux" or not os.environ.get("OPENCODE_TEST_BINARY"),
                    reason="requires explicit pinned Linux OpenCode binary")
async def test_native_servers_read_complete_history_after_round_trip(tmp_path):
    identity = Identity(frontend_id="web", user_id="alice")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    processes, clients, paths = [], [], []
    try:
        for name in ["a", "b"]:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            home = tmp_path / name
            env = {key: os.environ[key] for key in ["PATH", "TMPDIR"] if key in os.environ}
            env.update({"HOME": str(home), "XDG_DATA_HOME": str(home / "data"),
                "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache"),
                "XDG_STATE_HOME": str(home / "state"),
                "OPENCODE_CONFIG_CONTENT": json.dumps({"autoupdate": False, "plugin": []})})
            processes.append(await asyncio.create_subprocess_exec(os.environ["OPENCODE_TEST_BINARY"],
                "serve", "--hostname", "127.0.0.1", "--port", str(port), env=env, cwd=workspace,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE))
            client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                                       params={"directory": str(workspace)}, timeout=10, trust_env=False)
            clients.append(client)
            for _ in range(200):
                if processes[-1].returncode is not None:
                    pytest.fail((await processes[-1].stderr.read()).decode())
                try:
                    response = await client.get("/global/health")
                    response.raise_for_status()
                    break
                except (httpx.TransportError, httpx.HTTPStatusError):
                    await asyncio.sleep(0.1)
            else:
                pytest.fail("OpenCode did not become healthy")
            response = await client.get("/path")
            response.raise_for_status()
            paths.append(home / "data/opencode/opencode.db")
        context = {**identity.model_dump(), "identity_id": identity.identity_id,
                   "chat_id": "chat", "schema_version": 1, "bucket_id": "a", "requests": {}}
        response = await clients[0].post("/session", json={"title": "Migration", "permission": [],
                                            "metadata": {"bucket_runtime": context}})
        response.raise_for_status()
        session = response.json()["id"]
        message = {"role": "user", "time": {"created": 1234}, "agent": "build",
                   "model": {"providerID": "test", "modelID": "test"}}
        with database(paths[0]) as db:
            db.execute("INSERT INTO message (id,session_id,time_created,time_updated,data) VALUES (?,?,?,?,?)",
                       ("msg_test_history", session, 1234, 1234, json.dumps(message)))
            db.execute("INSERT INTO part (id,message_id,session_id,time_created,time_updated,data) VALUES (?,?,?,?,?,?)",
                       ("prt_test_history", "msg_test_history", session, 1234, 1234,
                        json.dumps({"type": "text", "text": "Preserve this native history"})))
        for source, target in [(0, 1), (1, 0)]:
            history = export_history(paths[source], session, identity, "chat")
            validate_destination(paths[target], history)
            response = await clients[source].delete(f"/session/{session}")
            response.raise_for_status()
            with database(paths[source]) as db:
                assert db.execute("SELECT COUNT(*) FROM session WHERE id=?", (session,)).fetchone()[0] == 0
            import_history(paths[target], history, identity, "chat", "ab"[target])
            response = await clients[target].get(f"/session/{session}/message")
            response.raise_for_status()
            messages = response.json()
            assert messages[0]["parts"][0]["text"] == "Preserve this native history"
            response = await clients[target].get(f"/session/{session}")
            response.raise_for_status()
            assert response.json()["metadata"]["bucket_runtime"]["bucket_id"] == "ab"[target]
        final = export_history(paths[0], session, identity, "chat")
        delete_history(paths[0], final)
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
@pytest.mark.skipif(sys.platform != "linux" or not os.environ.get("OPENCODE_TEST_BINARY"),
                    reason="requires explicit pinned Linux OpenCode binary and built plugin")
async def test_capacity_reassignment_continues_history_through_real_runtimes(tmp_path):
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
    from websockets.asyncio.client import connect
    from agent_backend.config import ControlConfig, RuntimeConfig
    from agent_backend.wire import Peer
    from bucket_runtime.runtime import Runtime
    from bucket_runtime.server import rpc_app
    from bucket_runtime.supervisor import Supervisor
    from bucket_runtime.sandbox_executor import SandboxExecutor
    from container_up.server import create_app
    from support import register_bucket
    from test_live_opencode import Channels, port, running

    model_port, cu_port = port(), port()
    calls, runtimes, supervisors, tasks = [], [], [], []
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
    config_path.write_text(json.dumps({"autoupdate": False, "provider": {"test": {
        "npm": "@ai-sdk/openai-compatible", "name": "Local test",
        "options": {"baseURL": f"http://127.0.0.1:{model_port}/v1", "apiKey": "unused"},
        "models": {"test": {"name": "test", "limit": {"context": 32000, "output": 4096}}}}},
        "model": "test/test"}))
    control = ControlConfig(state_root=tmp_path / "control", frontends={"web": {"provider": "web"}},
                            bucket_pool={"max_users_per_bucket": 1}, limits={"request_seconds": 60})
    app = create_app(control, Channels())
    plugin = Path(__file__).resolve().parents[2] / "opencode_plugin/dist/index.mjs"
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(running(model, model_port))
        await stack.enter_async_context(running(app, cu_port))
        client = await stack.enter_async_context(httpx.AsyncClient(base_url=f"http://127.0.0.1:{cu_port}",
                                                                  timeout=70, trust_env=False))

        async def start_bucket(bucket_id):
            config = RuntimeConfig(frontend_id="web", bucket_id=bucket_id, common_root=common,
                state_root=tmp_path / "runtime/web" / bucket_id, workspace_root=tmp_path / "users",
                control_url=f"ws://127.0.0.1:{cu_port}/internal/buckets/ws", rpc_port=port(), health_port=port(),
                opencode={"binary": os.environ["OPENCODE_TEST_BINARY"], "port": port(),
                    "context_root": tmp_path / "context", "config_file": config_path,
                    "plugin_file": plugin, "dependency_dir": plugin.parent.parent})
            supervisor = Supervisor(config)
            supervisors.append(supervisor)
            runtime = Runtime(config, supervisor.client, SandboxExecutor(config.sandbox, config.limits.output_bytes))
            runtimes.append(runtime)
            await stack.enter_async_context(running(rpc_app(runtime, supervisor.rpc_token), config.rpc_port))
            await supervisor.start()
            (await supervisor.client.http.get("/agent")).raise_for_status()
            assert runtime.plugin_ready
            register_bucket(app.state.service, bucket_id)
            template = control.runtime_templates["web"]
            template.state_root, template.workspace_root = tmp_path / "runtime", tmp_path / "users"
            socket_ = await stack.enter_async_context(connect(config.control_url, proxy=None,
                                                              additional_headers={"X-Bucket-ID": bucket_id}))
            runtime.peer = Peer(socket_, runtime.db, bucket_id, runtime.on_command, runtime.session_operation)
            tasks.append(asyncio.create_task(runtime.peer.run()))
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
            response = await source.client.for_directory(tmp_path / "users/web/alice").http.get(
                f"/session/{session_id}", params={"directory": str(tmp_path / "users/web/alice")})
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
            for supervisor in supervisors:
                await supervisor.stop()
            for runtime in runtimes:
                runtime.db.close()
