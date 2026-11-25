"""Real native process + plugin + CU + Gateway, with a deterministic local model."""

import asyncio
import json
import os
import socket
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from agent_backend.config import BucketConfig, ControlConfig, GatewaySettings
from agent_backend.models import Attachment, Submit, digest
from agent_backend.native_sse import messages
from agent_backend.opencode import OpenCode
from agent_backend.session_history import verify_history
from channel_gateway.server import create_app as gateway_app
from container_up.bucket_manager import environment
from container_up.server import create_app


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def running(app, listen_port):
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=listen_port, log_level="warning", lifespan="on")
    )
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.05)
        yield
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@asynccontextmanager
async def native_process(config, password="test-password"):
    config.opencode.context_root.mkdir(parents=True, exist_ok=True)
    log = (config.opencode.context_root / "server.log").open("wb")
    process = await asyncio.create_subprocess_exec(
        str(config.opencode.binary),
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        str(config.opencode.port),
        cwd=config.opencode.context_root,
        env={**os.environ, **environment(config, password)},
        stdout=log,
        stderr=log,
    )
    endpoint = {"url": f"http://127.0.0.1:{config.opencode.port}", "password": password}
    client = OpenCode(endpoint)
    try:
        async with asyncio.timeout(40):
            while True:
                if process.returncode is not None:
                    raise RuntimeError((config.opencode.context_root / "server.log").read_text())
                try:
                    if await client.ready():
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
        yield client, endpoint
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 8)
            except TimeoutError:
                process.kill()
                await process.wait()
        await client.close()
        log.close()


class Channels:
    async def start(self, dispatch):
        pass

    async def stop(self):
        pass

    async def deliver(self, row, result):
        pass


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform != "linux" or not os.environ.get("OPENCODE_TEST_BINARY"),
    reason="requires Linux bwrap and the pinned native executable",
)
async def test_direct_native_queue_permissions_attachment_and_cancel(tmp_path):
    model_port, cu_port, gw_port = port(), port(), port()
    common = tmp_path / "common"
    public = common / "web/app"
    public.mkdir(parents=True)
    private = common / "web/config"
    private.mkdir()
    (public / "public.txt").write_text("public-sentinel")
    secret = private / "secret.txt"
    secret.write_text("private-secret")
    permissions = private / "permissions.yaml"
    permissions.write_text(
        "users:\n  - user_id: restricted\n    filesystem: {workspace: read_only}\n"
    )
    model_file = private / "opencode.json"
    model_file.write_text(
        json.dumps(
            {
                "update": "disable",
                "snapshots": False,
                "model": "test/test",
                "agents": {"worker": {"mode": "subagent", "description": "test worker"}},
                "providers": {
                    "test": {
                        "package": "@opencode/ai/providers/openai-compatible",
                        "settings": {
                            "baseURL": f"http://127.0.0.1:{model_port}/v1",
                            "apiKey": "test-only",
                        },
                        "models": {
                            "test": {"name": "test", "limit": {"context": 32000, "output": 4096}}
                        },
                    }
                },
            }
        )
    )
    config = BucketConfig(
        frontend_id="web",
        bucket_id="b",
        common_root=common,
        workspace_root=tmp_path / "users",
        state_root=tmp_path / "native",
        permissions_file=permissions,
        gateway_url=f"http://127.0.0.1:{gw_port}",
        opencode={
            "binary": os.environ["OPENCODE_TEST_BINARY"],
            "port": port(),
            "context_root": tmp_path / "context",
            "config_file": model_file,
            "plugin_dir": Path(__file__).resolve().parents[2] / "opencode_plugin",
        },
    )
    control = ControlConfig(
        state_root=tmp_path / "cu",
        frontends={"web": {"provider": "web"}},
        bucket_templates={"web": config},
        frontend_idle_ttl_seconds={"web": 0},
        limits={"request_seconds": 60},
    )
    cu = create_app(control)
    gw = gateway_app(
        control,
        GatewaySettings(state_root=tmp_path / "gw", control_url=f"http://127.0.0.1:{cu_port}"),
        Channels(),
    )
    service = cu.state.service
    uploaded = []

    async def upload(row, attachment_id, data, filename, content_type, checksum):
        uploaded.append(bytes(data))
        value = Attachment(
            attachment_id=attachment_id,
            url="https://example.test/file",
            filename=filename,
            content_type=content_type,
            size=len(data),
            sha256=checksum,
            storage="test",
        )
        gw.state.service.db.put(
            "artifact",
            digest([row["frontend_id"], row["request_id"], attachment_id]),
            {"request_id": row["request_id"], "attachment": value.model_dump()},
        )
        return value

    gw.state.service.artifacts.upload = upload
    model = FastAPI()
    calls = []
    child_id = None

    @model.post("/v1/chat/completions")
    async def completion(request: Request):
        body = await request.json()
        calls.append(body)
        users = [i for i, message in enumerate(body["messages"]) if message["role"] == "user"]
        text = str(body["messages"][users[-1]]["content"])
        results = [m for m in body["messages"][users[-1] + 1 :] if m["role"] == "tool"]
        name = args = None
        if text.startswith("You are a subagent") or text == "CHILD_WORK":
            if not results:
                name, args = (
                    "shell",
                    {
                        "command": "sleep 0.2; printf child >> child.txt",
                        "description": "Child write",
                    },
                )
        elif text.startswith("DELEGATE") and not results:
            name, args = (
                "subagent",
                {"agent": "worker", "description": "Run child work", "prompt": "CHILD_WORK"},
            )
            if "AGAIN" in text:
                args["sessionID"] = child_id
        elif "REPORT" in text:
            actions = [
                (
                    "shell",
                    {
                        "command": f"test ! -e '{secret}' && printf report > report.txt",
                        "description": "Write report",
                    },
                ),
                ("read", {"path": str(secret)}),
                ("read", {"path": str(public / "public.txt")}),
                ("publish_attachment", {"files": [{"path": "report.txt"}]}),
            ]
            if len(results) < len(actions):
                name, args = actions[len(results)]
            if len(results) >= 2:
                assert "private-secret" not in str(body["messages"])
                assert "permission scope" in str(results[1])
            if len(results) >= 3:
                assert "public-sentinel" in str(results[2])
            if len(results) >= 4:
                assert "queued_for_reply" in str(results[3]), results[3]
        elif "LONG" in text and not results:
            name, args = (
                "shell",
                {"command": "touch started; sleep 30; touch survived", "description": "Long task"},
            )
        elif "RESTRICTED" in text and not results:
            name, args = "write", {"path": "forbidden", "content": "no"}
        delta = (
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_" + str(len(calls)),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ]
            }
            if name
            else {"content": "answer:" + text}
        )

        async def stream():
            if "FIRST" in text:
                await asyncio.sleep(1)
            for content, finish in [(delta, None), ({}, "tool_calls" if name else "stop")]:
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "id": "test",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "test",
                            "choices": [{"index": 0, "delta": content, "finish_reason": finish}],
                            "usage": {
                                "prompt_tokens": 7,
                                "completion_tokens": 3,
                                "total_tokens": 10,
                            },
                        }
                    )
                    + "\n\n"
                )
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    async with running(model, model_port), native_process(config) as (native, endpoint):
        service.db.put(
            "bucket",
            "b",
            {
                "bucket_id": "b",
                "frontend_id": "web",
                "behavior": "pool",
                "owner": None,
                "status": "running",
                "password": endpoint["password"],
                "opencode": endpoint,
                "last_active_at": time.time(),
            },
        )
        service.pool.manager.exists = AsyncMock(return_value=True)
        service.pool.manager.endpoint = AsyncMock(return_value=endpoint)
        service.pool.manager.ensure = AsyncMock()
        async with (
            running(cu, cu_port),
            running(gw, gw_port),
            httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{gw_port}", timeout=80, trust_env=False
            ) as http,
        ):

            async def submit(request_id, text, user="alice", chat="chat", sync=True):
                response = await http.post(
                    "/inbound-sync/web" if sync else "/inbound/web",
                    json={
                        "request_id": request_id,
                        "user_id": user,
                        "chat_id": chat,
                        "content": text,
                    },
                )
                assert response.status_code == 200, response.text
                return response.json()

            report = await submit("report", "REPORT")
            assert report["status"] == "completed", report
            assert uploaded == [b"report"]
            assert len(report["attachments"]) == 1
            count = len(calls)
            assert (await submit("report", "REPORT"))["content"] == report["content"]
            assert len(calls) == count
            one, two = await asyncio.gather(submit("first", "FIRST"), submit("second", "SECOND"))
            assert one["status"] == two["status"] == "completed", (one, two)
            assert "FIRST" in one["content"] and "SECOND" not in one["content"]
            assert "SECOND" in two["content"] and "FIRST" not in two["content"]
            assert (
                service.db.get("request", "first")["native"]["session_id"]
                == service.db.get("request", "second")["native"]["session_id"]
            )
            other = await submit("other", "OTHER", chat="other-chat")
            assert other["status"] == "completed", other
            assert (
                service.db.get("request", "other")["native"]["session_id"]
                != service.db.get("request", "first")["native"]["session_id"]
            )
            changed = json.loads(model_file.read_text())
            changed["providers"]["test"]["models"]["test2"] = {
                "name": "test2",
                "limit": {"context": 32000, "output": 4096},
            }
            changed["model"] = "test/test2"
            model_file.write_text(json.dumps(changed))
            async with asyncio.timeout(15):
                while (await native.call("GET", "/api/model/default"))["data"]["id"] != "test2":
                    await asyncio.sleep(0.1)
            reloaded = await submit("reload", "RELOAD", chat="fresh-model")
            assert reloaded["status"] == "completed", reloaded
            assert calls[-1]["model"] == "test2"
            restricted = await submit("restricted", "RESTRICTED", user="restricted")
            assert restricted["status"] == "completed", restricted
            assert not (config.workspace_root / "web/restricted/forbidden").exists()
            delegated = await submit("delegate", "DELEGATE")
            assert delegated["status"] == "completed", delegated
            sid = service.db.get("request", "delegate")["native"]["session_id"]
            children = await native.children(sid)
            assert len(children) == 1
            child_id = children[0]["id"]
            assert (config.workspace_root / "web/alice/child.txt").read_text() == "child"
            again = await submit("delegate-again", "DELEGATE AGAIN")
            assert again["status"] == "completed", again
            assert (config.workspace_root / "web/alice/child.txt").read_text() == "childchild"
            child_inputs = [m for m in await messages(native.http, child_id) if m["type"] == "user"]
            assert [m["metadata"]["managed"]["request_id"] for m in child_inputs] == [
                "delegate",
                "delegate-again",
            ]
            # Policy changes affect an existing native session without reset/RPC.
            permissions.write_text(
                "users:\n  - user_id: restricted\n    filesystem: {workspace: read_write}\n"
            )
            writable = await submit("now-writable", "RESTRICTED", user="restricted")
            assert writable["status"] == "completed", writable
            assert (config.workspace_root / "web/restricted/forbidden").read_text() == "no"
            await submit("cancel", "LONG", user="charlie", sync=False)
            workspace = config.workspace_root / "web/charlie"
            async with asyncio.timeout(15):
                while not (workspace / "started").exists():
                    await asyncio.sleep(0.1)
            response = await http.post("/requests/web/cancel/cancel")
            assert response.status_code == 200, response.text
            async with asyncio.timeout(15):
                while True:
                    cancelled = (await http.get("/requests/web/cancel")).json()
                    if cancelled["status"] == "cancelled":
                        break
                    await asyncio.sleep(0.1)
            assert not (workspace / "survived").exists()
            assert not service.db.records("opencode_event")
            async with asyncio.timeout(10):
                while not service.db.get("request", "delegate-again").get("output_collected"):
                    await asyncio.sleep(0.1)
            original = [await native.export_session(info["id"]) for info in await native.tree(sid)]
            second = config.model_copy(deep=True)
            second.bucket_id = "b2"
            second.state_root = tmp_path / "native2"
            second.opencode.context_root = tmp_path / "context2"
            second.opencode.port = port()
            async with native_process(second) as (target, target_endpoint):
                service.db.put(
                    "bucket",
                    "b2",
                    {
                        "bucket_id": "b2",
                        "frontend_id": "web",
                        "behavior": "pool",
                        "owner": None,
                        "status": "running",
                        "password": target_endpoint["password"],
                        "opencode": target_endpoint,
                        "last_active_at": time.time(),
                    },
                )
                service.pool.manager.endpoint = AsyncMock(
                    side_effect=lambda bucket_id, *_: (
                        endpoint if bucket_id == "b" else target_endpoint
                    )
                )
                business = Submit.model_validate(
                    service.db.get("request", "delegate-again")["submit"]
                )
                assert (
                    await service.sessions.prepare(
                        business, "b2", config.workspace_root / "web/alice"
                    )
                    == sid
                )
                assert not await native.tree(sid, missing_ok=True)
                for data in original:
                    verify_history(data, await target.export_session(data["info"]["id"]))
            # The native store and IDs survive replacing the entire server process.
            async with native_process(second) as (restarted, _):
                for data in original:
                    verify_history(data, await restarted.export_session(data["info"]["id"]))
