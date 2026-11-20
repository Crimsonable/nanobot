"""Pinned V2 transport, plugin registration, execution and native config watcher.

Set OPENCODE_TEST_BINARY to run; the model is a local deterministic HTTP server.
"""
import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from test_live_opencode import port, running

from agent_backend.config import RuntimeConfig
from agent_backend.models import Identity, Submit
from bucket_runtime.adapter import BucketRuntimeAdapter
from bucket_runtime.opencode_server import OpenCodeServer
from bucket_runtime.server import rpc_app


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("OPENCODE_TEST_BINARY"), reason="requires explicit OpenCode 2.x binary")
async def test_native_plugin_prompt_and_config_reload_without_restart(tmp_path):
    model_port, rpc_port, oc_port = port(), port(), port()
    model = FastAPI()
    calls = []
    model_streaming = asyncio.Event()

    @model.post("/v1/chat/completions")
    async def completion(request: Request):
        body = await request.json()
        calls.append(body)
        system = str([message["content"] for message in body["messages"] if message["role"] == "system"])
        assert f"WORKSPACE_RULE_{body['model']}" in system
        assert "namespace opencode" not in system
        user_at = max((i for i, message in enumerate(body["messages"]) if message["role"] == "user"), default=-1)
        if "CANCEL_PROBE" in str(body["messages"][user_at]["content"]):
            async def pending():
                model_streaming.set()
                yield ': waiting for interruption\n\n'
                await asyncio.Event().wait()
            return StreamingResponse(pending(), media_type="text/event-stream")
        tool_results = [m for m in body["messages"][user_at + 1:] if m["role"] == "tool"]
        can_read = any(tool.get("function", {}).get("name") == "read" for tool in body.get("tools", []))
        if can_read and not tool_results:
            deltas = [({"role": "assistant", "tool_calls": [{"index": 0, "id": "read_" + str(len(calls)),
                "type": "function", "function": {"name": "read", "arguments": '{"path":"fixture.txt"}'}}]}, None),
                ({}, "tool_calls")]
        else:
            if tool_results:
                assert "fixture-sentinel" in str(tool_results[-1])
            deltas = [({"role": "assistant", "content": "native answer"}, None), ({}, "stop")]
        async def chunks():
            for delta, finish in deltas:
                chunk = {"id": "test", "object": "chat.completion.chunk", "created": 1,
                    "model": body["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                if finish:
                    chunk["usage"] = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
                yield "data: " + json.dumps(chunk) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(chunks(), media_type="text/event-stream")

    config_path = tmp_path / "model.json"
    native = {"update": "disable", "snapshots": False, "model": "test/first",
        "providers": {"test": {"package": "@opencode/ai/providers/openai-compatible",
            "settings": {"baseURL": f"http://127.0.0.1:{model_port}/v1", "apiKey": "test-only"},
            "models": {name: {"name": name, "limit": {"context": 32000, "output": 4096}}
                       for name in ["first", "second"]}}}}
    config_path.write_text(json.dumps(native), encoding="utf-8")
    config = RuntimeConfig(frontend_id="web", common_root=tmp_path / "common", bucket_id="live",
        state_root=tmp_path / "state", workspace_root=tmp_path / "users", rpc_port=rpc_port,
        health_port=port(), opencode={"binary": Path(os.environ["OPENCODE_TEST_BINARY"]),
            "port": oc_port, "context_root": tmp_path / "context", "config_file": config_path,
            "plugin_dir": Path(__file__).resolve().parents[2] / "opencode_plugin"})
    client = OpenCodeServer(config)
    async def kill(_):
        pass
    runtime = BucketRuntimeAdapter(config, client, SimpleNamespace(kill=kill))
    processing = asyncio.create_task(runtime.process_events())
    try:
        async with running(model, model_port), running(rpc_app(runtime, client.rpc_token), rpc_port):
            await client.start(runtime.on_opencode_event)
            pid = client.process.pid
            assert client.ready and client.plugin_ready.is_set()
            for index, name in enumerate(["first", "second"]):
                if index:
                    native["model"] = "test/second"
                    replacement = config_path.with_suffix(".new")
                    replacement.write_text(json.dumps(native), encoding="utf-8")
                    replacement.replace(config_path)
                    async with asyncio.timeout(20):
                        while True:
                            response = await client.http.get("/api/model/default", params=client._params())
                            if response.json().get("data", {}).get("id") == "second":
                                break
                            await asyncio.sleep(0.2)
                submit = Submit(request_id=f"r{index}", chat_id="chat",
                    identity=Identity(frontend_id="web", user_id="alice"), content="say hello",
                    deadline_at=time.time() + 45)
                runtime.db.put("run", submit.request_id, {"request_id": submit.request_id, "status": "queued",
                    "submit": submit.model_dump(mode="json"), "artifacts": []})
                workspace = runtime.workspace(submit)
                (workspace / "fixture.txt").write_text("fixture-sentinel")
                (workspace / "AGENTS.md").write_text(f"WORKSPACE_RULE_{name}")
                # Rules may change, but workspace config must never load plugins.
                (workspace / "opencode.json").write_text(json.dumps({"plugins": ["file:///untrusted-plugin"]}))
                await runtime.run(submit.request_id)
                row = runtime.db.get("run", submit.request_id)
                assert row["status"] == "completed", row.get("result")
                assert any(call["model"] == name for call in calls)
                assert client.process.pid == pid
                assert not runtime.db.records("native_failed")
            sessions = runtime.db.records("session")
            assert len(sessions) == 1
            assert any(e.type == "opencode.event" and e.payload["event"]["type"] == "session.tool.success"
                       for e in runtime.db.pending("gateway"))
            assert await client.children(sessions[0]["session_id"]) == []
            # Interrupt a live model stream through the same adapter path as /cancel.
            cancel_probe = submit.model_copy(update={"request_id": "cancel-probe", "content": "CANCEL_PROBE",
                                                     "deadline_at": time.time() + 45})
            runtime.db.put("run", cancel_probe.request_id, {"request_id": cancel_probe.request_id,
                "status": "queued", "submit": cancel_probe.model_dump(mode="json"), "artifacts": []})
            runtime.launch(cancel_probe.request_id)
            try:
                await asyncio.wait_for(model_streaming.wait(), 20)
                await asyncio.wait_for(runtime.cancel_request(cancel_probe.request_id), 10)
                assert runtime.db.get("run", cancel_probe.request_id)["status"] == "cancelled"
                assert not runtime.draining
            finally:
                for task in list(runtime.tasks.values()):
                    task.cancel()
                await asyncio.gather(*runtime.tasks.values(), return_exceptions=True)
            # An already idle session must also accept interrupt followed by wait.
            await client.abort(sessions[0]["session_id"])
            await client.delete_session(sessions[0]["session_id"])
    finally:
        processing.cancel()
        await asyncio.gather(processing, return_exceptions=True)
        await client.close()
        runtime.db.close()
