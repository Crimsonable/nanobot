"""Real pinned OpenCode + plugin + bwrap + WebSocket test; no paid/external model.

Run on Linux with OPENCODE_TEST_BINARY=/path/to/opencode and a built plugin.
"""
import asyncio
import json
import os
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from websockets.asyncio.client import connect

from agent_backend.config import ControlConfig, RuntimeConfig
from agent_backend.wire import Peer
from bucket_runtime.runtime import Runtime
from bucket_runtime.sandbox_executor import SandboxExecutor
from bucket_runtime.server import rpc_app
from bucket_runtime.supervisor import Supervisor
from container_up.server import create_app

USED_PORTS: set[int] = set()


def port():
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            candidate = sock.getsockname()[1]
        if candidate not in USED_PORTS:
            USED_PORTS.add(candidate)
            return candidate


@asynccontextmanager
async def running(app, listen_port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=listen_port,
                                          log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            if task.done():
                task.result()
            await asyncio.sleep(0.05)
        assert server.started
        yield
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class Channels:
    async def start(self, dispatch):
        pass

    async def stop(self):
        pass

    async def deliver(self, row, result):
        raise AssertionError("synchronous requests must not be delivered to a channel")


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux" or not os.environ.get("OPENCODE_TEST_BINARY"),
                    reason="requires Linux, bwrap and explicit pinned OpenCode binary")
async def test_real_backend_attachment_and_isolation(tmp_path):
    binary = Path(os.environ["OPENCODE_TEST_BINARY"])
    plugin = Path(__file__).resolve().parents[2] / "opencode_plugin/dist/index.mjs"
    assert plugin.exists(), "run npm ci && npm run build in opencode_plugin"
    oc_port, rpc_port, cu_port, model_port = port(), port(), port(), port()
    common = tmp_path / "common"
    public = common / "web/app"
    public.mkdir(parents=True)
    (public / "SKILL.md").write_text("public skill")
    private_config = common / "web/config"
    private_config.mkdir()
    secret_file = private_config / "future-secret.txt"
    secret_file.write_text("frontend-config-must-stay-private")
    model_config = private_config / "opencode-server.json"
    model_config.write_text(json.dumps({"provider": {"test": {
        "npm": "@ai-sdk/openai-compatible", "name": "Offline test",
        "options": {"baseURL": f"http://127.0.0.1:{model_port}/v1", "apiKey": "unused"},
        "models": {"test": {"name": "test", "limit": {"context": 32000, "output": 4096}}},
    }}}))
    runtime_config = RuntimeConfig.model_validate({
        "bucket_id": "bucket-0", "control_url": f"ws://127.0.0.1:{cu_port}/internal/buckets/ws",
        "frontend_id": "web", "common_root": str(common),
        "state_root": str(tmp_path / "runtime"),
        "workspace_root": str(tmp_path / "workspaces"), "rpc_port": rpc_port, "health_port": port(),
        "opencode": {"binary": str(binary), "port": oc_port,
            "context_root": str(tmp_path / "context"), "config_file": str(model_config),
            "plugin_file": str(plugin), "dependency_dir": str(plugin.parent.parent)},
        "profiles": {"default": {"model": "test/test", "public_roots": [str(public)]}},
        "limits": {"request_seconds": 90},
    })
    control_config = ControlConfig.model_validate({
        "state_root": str(tmp_path / "control"), "bucket_ids": ["bucket-0"],
        "frontends": {"web": {"provider": "web", "profile_id": "default",
                              "channel_config": {}}},
        "limits": {"request_seconds": 90},
    })
    app = create_app(control_config, Channels())
    uploaded = []

    async def upload(identity, upload_id, path, attachment):
        uploaded.append(path.read_bytes())
        result = attachment.model_copy(update={"url": "https://files.invalid/" + upload_id,
                                               "object_key": upload_id, "storage": "minio"})
        runtime.db.put("artifact", upload_id, {"owner": identity.key, "attachment": result.model_dump()})
        return result

    model = FastAPI()
    model_calls = []
    input_downloads = []

    @model.get("/input.txt")
    async def input_file():
        input_downloads.append(True)
        raise AssertionError("CU/runtime must not pre-download attachment references")

    @model.post("/v1/chat/completions")
    async def completion(request: Request):
        body = await request.json()
        model_calls.append(body)
        tool_messages = [m for m in body["messages"] if m["role"] == "tool"]
        if not tool_messages:
            name = "bash"
            args = {"command": f"test ! -e '{private_config}' && printf 'generated report' > output/report.txt && cat "
                    + str(public / "SKILL.md"), "description": "Generate report"}
            if any("Long task" in str(m.get("content")) for m in body["messages"]):
                args = {"command": "sleep 30", "description": "Long task"}
        elif len(tool_messages) == 1:
            name = "read"
            args = {"filePath": "output/report.txt"}
        elif len(tool_messages) == 2:
            assert "generated report" in str(tool_messages[-1]["content"]), tool_messages[-1]
            name = "read"
            args = {"filePath": str(secret_file)}
        elif len(tool_messages) == 3:
            assert "Runtime rejected /authorize: 403" in str(tool_messages[-1]["content"]), tool_messages[-1]
            assert "frontend-config-must-stay-private" not in str(body["messages"])
            name = "read"
            args = {"filePath": str(public / "SKILL.md")}
        elif len(tool_messages) == 4:
            assert "public skill" in str(tool_messages[-1]["content"]), tool_messages[-1]
            name = "publish_attachment"
            args = {"files": [{"path": "output/report.txt", "filename": "report.txt"}]}
        else:
            name = None
            args = None
        chunks = []
        if name:
            chunks.append({"tool_calls": [{"index": 0, "id": "call_" + str(len(tool_messages)),
                "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]})
        else:
            chunks.append({"content": "Report ready."})

        async def stream():
            for delta in chunks:
                yield "data: " + json.dumps({"id": "chatcmpl-test", "object": "chat.completion.chunk",
                    "created": 1, "model": "test", "choices": [{"index": 0, "delta": delta,
                    "finish_reason": None}]}) + "\n\n"
            yield "data: " + json.dumps({"id": "chatcmpl-test", "object": "chat.completion.chunk",
                "created": 1, "model": "test", "choices": [{"index": 0, "delta": {},
                "finish_reason": "tool_calls" if name else "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream")

    supervisor = Supervisor(runtime_config)
    executor = SandboxExecutor(runtime_config.sandbox, runtime_config.limits.output_bytes)
    runtime = Runtime(runtime_config, supervisor.client, executor)
    runtime.artifacts.upload = upload
    peer_task = event_task = None
    async with running(model, model_port), running(app, cu_port), running(rpc_app(runtime, supervisor.rpc_token), rpc_port):
        try:
            await executor.probe(tmp_path / "probe")
            await supervisor.start()
            initialized = await supervisor.client.http.get("/config")
            assert initialized.status_code == 200, initialized.text
            # The plugin initializes on tool discovery, not necessarily on config access.
            initialized = await supervisor.client.http.get("/agent")
            assert initialized.status_code == 200, initialized.text
            assert runtime.plugin_ready
            async with connect(runtime_config.control_url, proxy=None, additional_headers={
                "X-Bucket-ID": "bucket-0"}) as socket_:
                runtime.peer = Peer(socket_, runtime.db, "bucket-0", runtime.on_command, runtime.rpc)
                peer_task = asyncio.create_task(runtime.peer.run())
                event_task = asyncio.create_task(runtime.events())
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{cu_port}",
                    timeout=100, trust_env=False) as client:
                    payload = {"user_id": "alice", "content": "Generate the report", "request_id": "req-test",
                        "attachments": [{"url": f"http://127.0.0.1:{model_port}/input.txt",
                                         "filename": "input.txt", "content_type": "text/plain"}]}
                    response = await client.post("/inbound-sync/web", json=payload)
                    assert response.status_code == 200, response.text
                    result = response.json()
                    assert result["status"] == "completed", json.dumps(result)
                    assert result["content"] == "Report ready."
                    assert result["attachments"][0]["filename"] == "report.txt"
                    assert result["usage"]["output_tokens"] > 0
                    assert uploaded == [b"generated report"]
                    assert not input_downloads
                    alice_row = runtime.db.get("run", "req-test")
                    from agent_backend.models import Submit
                    alice_workspace = runtime.workspace(Submit.model_validate(alice_row["submit"]))
                    assert not (alice_workspace / "inbox").exists()
                    assert any(f"http://127.0.0.1:{model_port}/input.txt" in str(call["messages"]) for call in model_calls)
                    again = await client.post("/inbound-sync/web", json=payload)
                    assert again.json()["request_id"] == result["request_id"]
                    assert len(uploaded) == 1
                    # A second user shares the server, but owns another session and workspace.
                    other = await client.post("/inbound-sync/web", json={"user_id": "bob",
                        "content": "Generate the report", "request_id": "req-bob"})
                    assert other.json()["status"] == "completed", other.text
                    bob_row = runtime.db.get("run", "req-bob")
                    assert bob_row["session_id"] != alice_row["session_id"]
                    assert other.json()["attachments"][0]["attachment_id"] != result["attachments"][0]["attachment_id"]
                    # Cancel a live sandbox command and wait for a durable terminal result.
                    await client.post("/inbound/web", json={"user_id": "charlie", "content": "Long task",
                        "request_id": "req-cancel"})
                    for _ in range(100):
                        if executor.processes.get("req-cancel"):
                            break
                        await asyncio.sleep(0.1)
                    assert executor.processes.get("req-cancel")
                    await client.post("/requests/web/req-cancel/cancel")
                    for _ in range(100):
                        cancelled = (await client.get("/requests/web/req-cancel")).json()
                        if cancelled["status"] == "cancelled":
                            break
                        await asyncio.sleep(0.1)
                    assert cancelled["status"] == "cancelled", cancelled
                    assert not any(p.returncode is None for p in executor.processes.get("req-cancel", []))
                    row = runtime.db.get("run", "req-test")
                    with pytest.raises(PermissionError):
                        runtime.authorize(row["session_id"], "late", "bash", {"command": "echo bad"})
                    # Independent bwrap check: another user's directory and runtime secrets are absent.
                    private = tmp_path / "other-user"
                    private.mkdir()
                    (private / "secret").write_text("private")
                    own = tmp_path / "own"
                    own.mkdir()
                    check = await executor.execute("isolation", own, [public],
                        f"test ! -e '{private}' && test ! -e '{private_config}' && cat '{public / 'SKILL.md'}'")
                    assert check == {"output": "public skill", "exit_code": 0}
        finally:
            await runtime.shutdown()
            for task in [peer_task, event_task]:
                if task:
                    task.cancel()
            await asyncio.gather(*(t for t in [peer_task, event_task] if t), return_exceptions=True)
            await supervisor.stop()
            runtime.db.close()
