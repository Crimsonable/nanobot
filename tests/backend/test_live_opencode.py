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

from support import register_bucket
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
async def test_real_backend_attachment_and_isolation(tmp_path, capfd):
    binary = Path(os.environ["OPENCODE_TEST_BINARY"])
    plugin = Path(__file__).resolve().parents[2] / "opencode_plugin/dist/index.mjs"
    assert plugin.exists(), "run npm ci && npm run build in opencode_plugin"
    oc_port, rpc_port, cu_port, model_port = port(), port(), port(), port()
    common = tmp_path / "common"
    public = common / "web/app"
    public.mkdir(parents=True)
    (public / "SKILL.md").write_text("public skill")
    templates = public / "templates"
    templates.mkdir()
    (templates / "AGENTS.md").write_text("FRONTEND_TEMPLATE_RULE_78421")
    private_config = common / "web/config"
    private_config.mkdir()
    secret_file = private_config / "future-secret.txt"
    secret_file.write_text("frontend-config-must-stay-private")
    model_config = private_config / "opencode-server.json"
    model_config.write_text(json.dumps({"provider": {"test": {
        "npm": "@ai-sdk/openai-compatible", "name": "Offline test",
        "options": {"baseURL": f"http://127.0.0.1:{model_port}/v1", "apiKey": "unused"},
        "models": {"test": {"name": "test", "limit": {"context": 32000, "output": 4096}}},
    }}, "model": "test/test", "instructions": [str(templates / "AGENTS.md")]}))
    runtime_config = RuntimeConfig.model_validate({
        "bucket_id": "bucket-0", "control_url": f"ws://127.0.0.1:{cu_port}/internal/buckets/ws",
        "frontend_id": "web", "common_root": str(common),
        "state_root": str(tmp_path / "runtime"),
        "workspace_root": str(tmp_path / "workspaces"), "rpc_port": rpc_port, "health_port": port(),
        "opencode": {"binary": str(binary), "port": oc_port,
            "context_root": str(tmp_path / "context"), "config_file": str(model_config),
            "plugin_file": str(plugin), "dependency_dir": str(plugin.parent.parent)},
        "limits": {"request_seconds": 90},
    })
    control_config = ControlConfig.model_validate({
        "state_root": str(tmp_path / "control"),
        "frontends": {"web": {"provider": "web", "channel_config": {}}},
        "limits": {"request_seconds": 90},
    })
    app = create_app(control_config, Channels())
    register_bucket(app.state.service, "bucket-0")
    uploaded = []

    async def upload(identity, attachment_id, path, attachment):
        uploaded.append(path.read_bytes())
        result = attachment.model_copy(update={"url": "https://files.invalid/" + attachment_id,
                                               "object_key": attachment_id, "storage": "minio"})
        runtime.db.put("artifact", attachment_id, {"identity_id": identity.identity_id, "attachment": result.model_dump()})
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
        for message in body["messages"]:
            for call in message.get("tool_calls", []):
                if call.get("function", {}).get("name") == "bash":
                    assert not json.loads(call["function"]["arguments"])["command"].startswith("exec '/usr/bin/flock'")
        system = str([m["content"] for m in body["messages"] if m["role"] == "system"])
        assert "FRONTEND_TEMPLATE_RULE_78421" in system, system
        bob_request = any("BOB_REQUEST" in str(m.get("content")) for m in body["messages"] if m["role"] == "user")
        assert ("BOB_ONLY_RULE_96431" in system) == bob_request, system
        tool_messages = [m for m in body["messages"] if m["role"] == "tool"]
        if not tool_messages:
            name = "bash"
            args = {"command": f"test ! -e '{private_config}' && printf 'generated report' > report.txt && cat "
                    + str(public / "SKILL.md"), "description": "Generate report"}
            if any("Long task" in str(m.get("content")) for m in body["messages"]):
                args = {"command": "touch started; sleep 30; touch survived", "description": "Long task"}
        elif len(tool_messages) == 1:
            name = "read"
            args = {"filePath": "report.txt"}
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
            args = {"files": [{"path": "report.txt", "filename": "report.txt"}]}
        else:
            name = None
            args = None
        chunks = [{"reasoning_content": "Planning test step."}]
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
    def forbid_reply_queries(request):
        assert not request.url.path.endswith("/message"), "runtime must not query replies"
        assert request.url.path != "/global/event", "runtime must not subscribe to SSE"
    async def check_request(request):
        forbid_reply_queries(request)
    supervisor.client.http.event_hooks["request"].append(check_request)

    peer_task = None
    async with running(model, model_port), running(app, cu_port), running(rpc_app(runtime, supervisor.rpc_token), rpc_port):
        event_task = asyncio.create_task(runtime.plugin_events.run())
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
                runtime.peer = Peer(socket_, runtime.db, "bucket-0", runtime.on_command)
                peer_task = asyncio.create_task(runtime.peer.run())
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{cu_port}",
                    timeout=100, trust_env=False, headers={"Connection": "close"}) as client:
                    payload = {"chat_id": "chat", "user_id": "alice", "content": "Generate the report", "request_id": "req-test",
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
                    raw_events = app.state.service.db.records("opencode_event")
                    own_events = [e["payload"]["event"] for e in raw_events if e["request_id"] == "req-test"]
                    assert any(e["type"] == "message.part.updated" and
                               e["properties"]["part"]["type"] == "reasoning" for e in own_events), own_events
                    assert any(e["type"] == "message.part.delta" for e in own_events)
                    assert any(e["type"] == "tool.execute.before" for e in own_events)
                    assert any(e["payload"]["event"]["type"] == "session.created" for e in raw_events)
                    alice_row = runtime.db.get("run", "req-test")
                    session_info = await runtime.run_client(alice_row).session(alice_row["session_id"])
                    context = session_info["metadata"]["bucket_runtime"]
                    assert context["frontend_id"] == "web" and context["user_id"] == "alice"
                    assert context["chat_id"] == "chat"
                    assert "conversation_id" not in context and "conversation_id" not in result
                    assert context["requests"][alice_row["message_id"]] == "req-test"
                    from agent_backend.models import Submit
                    alice_workspace = runtime.workspace(Submit.model_validate(alice_row["submit"]))
                    assert not (alice_workspace / "inbox").exists()
                    assert session_info["directory"] == str(alice_workspace)
                    assert not (alice_workspace / "AGENTS.md").exists()
                    assert any(f"http://127.0.0.1:{model_port}/input.txt" in str(call["messages"]) for call in model_calls)
                    again = await client.post("/inbound-sync/web", json=payload)
                    assert again.json()["request_id"] == result["request_id"]
                    assert len(uploaded) == 1
                    # Per-user rules remain independent of the shared instructions file.
                    # Exercise different directory instances concurrently in this one server.
                    bob_workspace = runtime_config.workspace_root / "web" / "bob"
                    bob_workspace.mkdir(parents=True)
                    (bob_workspace / "AGENTS.md").write_text("BOB_ONLY_RULE_96431")
                    other, followup = await asyncio.gather(
                        client.post("/inbound-sync/web", json={"chat_id": "chat", "user_id": "bob",
                            "content": "Generate the report BOB_REQUEST", "request_id": "req-bob"}),
                        client.post("/inbound-sync/web", json={"chat_id": "chat", "user_id": "alice",
                            "content": "Say the result again", "request_id": "req-alice-next"}))
                    (templates / "AGENTS.md").write_text("FRONTEND_TEMPLATE_RULE_78421")
                    assert other.json()["status"] == "completed", other.text
                    bob_row = runtime.db.get("run", "req-bob")
                    assert bob_row["session_id"] != alice_row["session_id"]
                    assert other.json()["attachments"][0]["attachment_id"] != result["attachments"][0]["attachment_id"]
                    assert followup.json()["status"] == "completed", followup.text
                    assert runtime.db.get("run", "req-alice-next")["session_id"] == alice_row["session_id"]
                    session_info = await runtime.run_client(alice_row).session(alice_row["session_id"])
                    assert set(session_info["metadata"]["bucket_runtime"]["requests"].values()) == {"req-test", "req-alice-next"}
                    # A separate chat for the same user gets a different native session.
                    other_chat = await client.post("/inbound-sync/web", json={"user_id": "alice",
                        "chat_id": "group-2", "content": "Generate another report", "request_id": "req-alice-other"})
                    assert other_chat.json()["status"] == "completed", other_chat.text
                    other_row = runtime.db.get("run", "req-alice-other")
                    assert other_row["session_id"] not in {alice_row["session_id"], bob_row["session_id"]}
                    other_info = await runtime.run_client(other_row).session(other_row["session_id"])
                    assert other_info["metadata"]["bucket_runtime"]["chat_id"] == "group-2"
                    assert other_info["directory"] == str(alice_workspace)
                    bob_info = await runtime.run_client(bob_row).session(bob_row["session_id"])
                    assert bob_info["directory"] == bob_row["directory"]
                    assert bob_info["directory"] != str(alice_workspace)
                    # Restart the real OpenCode process with the same persisted home/session.
                    supervisor.process.terminate()
                    await asyncio.wait_for(supervisor.process.wait(), 10)
                    runtime.plugin_ready = False
                    await supervisor.start()
                    await supervisor.client.http.get("/agent")
                    assert runtime.plugin_ready
                    resumed = await client.post("/inbound-sync/web", json={"chat_id": "chat",
                        "user_id": "alice", "content": "Repeat after restart", "request_id": "req-resumed"})
                    assert resumed.json()["status"] == "completed", resumed.text
                    assert resumed.json()["content"] == "Report ready."
                    assert runtime.db.get("run", "req-resumed")["session_id"] == alice_row["session_id"]
                    # Cancel a live sandbox command and wait for a durable terminal result.
                    await client.post("/inbound/web", json={"chat_id": "chat", "user_id": "charlie", "content": "Long task",
                        "request_id": "req-cancel"})
                    cancel_workspace = runtime_config.workspace_root / "web" / "charlie"
                    for _ in range(100):
                        if (cancel_workspace / "started").exists():
                            break
                        await asyncio.sleep(0.1)
                    assert (cancel_workspace / "started").exists()
                    await client.post("/requests/web/req-cancel/cancel")
                    for _ in range(100):
                        cancelled = (await client.get("/requests/web/req-cancel")).json()
                        if cancelled["status"] == "cancelled":
                            break
                        await asyncio.sleep(0.1)
                    assert cancelled["status"] == "cancelled", cancelled
                    from bucket_runtime.file_lock import publication_lock
                    from agent_backend.models import Submit
                    cancelled_submit = Submit.model_validate(runtime.db.get("run", "req-cancel")["submit"])
                    async with asyncio.timeout(5):
                        async with publication_lock(runtime.user_lock_path(cancelled_submit)):
                            pass
                    assert not (cancel_workspace / "survived").exists()
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
    output = capfd.readouterr().out
    logs = [json.loads(line) for line in output.splitlines() if line.startswith('{"user_id":')]
    assert logs
    assert all(set(row) == {"user_id", "frontend_id", "chat_id", "content"} for row in logs)
    for user_id in ["alice", "bob"]:
        own_logs = [row for row in logs if row.get("user_id") == user_id]
        assert own_logs
        assert all(row["frontend_id"] == "web" and row["chat_id"] in {"chat", "group-2"} for row in own_logs)
        contents = [row["content"] for row in own_logs]
        assert any("Planning test step." in content for content in contents)
        assert any("Report ready." in content for content in contents)
        assert any("output" in content for content in contents)
