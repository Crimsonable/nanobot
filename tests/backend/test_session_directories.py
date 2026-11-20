import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from support import emit_answer

from agent_backend.config import RuntimeConfig
from agent_backend.models import Identity, Submit
from bucket_runtime.adapter import BucketRuntimeAdapter
from bucket_runtime.opencode_server import OpenCodeServer


@pytest.mark.asyncio
async def test_concurrent_users_chat_reuse_and_all_requests_keep_their_directory(tmp_path):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "AGENTS.md").write_text("managed rules")
    config = RuntimeConfig(frontend_id="web", common_root=tmp_path / "common", bucket_id="b", state_root=tmp_path / "state",
        workspace_root=tmp_path / "users",
        opencode={"context_root": tmp_path / "server"})
    sessions, calls = {}, []
    async def handle(request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        await asyncio.sleep(0)
        if path == "/api/session/active":
            return httpx.Response(200, json={"data": {}})
        if path.endswith("/inbox") or path == "/api/experimental/job":
            return httpx.Response(200, json={"data": []})
        if path == "/api/session" and request.method == "GET":
            return httpx.Response(200, json={"data": [], "cursor": {}})
        if path == "/api/model/default":
            directory = request.url.params["location[directory]"]
            calls.append((request.method, path, directory))
            return httpx.Response(200, json={"data": {"providerID":"intranet","id":"current-model"}})
        if path == "/api/session":
            directory = body["location"]["directory"]
            session_id = f"ses_{len(sessions)}"
            info = {"id":session_id,"location":body["location"],"permissions":body["permissions"],"metadata":body["metadata"]}
            sessions[session_id] = info
            return httpx.Response(200, json={"data":info})
        session_id = path.split("/")[-2] if path.endswith("/wait") else path.split("/")[3]
        info = sessions[session_id]
        directory = info["location"]["directory"]
        calls.append((request.method, path, directory))
        assert not request.url.params
        if path.endswith("/prompt"):
            emit_answer(runtime, session_id, body["id"], directory, message_id="answer_"+body["id"])
            return httpx.Response(200, json={"data":{}})
        if path.endswith("/model"):
            assert body == {"model":{"providerID":"intranet","id":"current-model"}}
            return httpx.Response(204)
        if request.method == "PATCH":
            info["metadata"] = body["metadata"]
            info["permissions"] = body["permissions"]
            return httpx.Response(204)
        return httpx.Response(200, json={"data":info})

    client = OpenCodeServer(config, "password")
    await client.http.aclose()

    async def kill(request_id):
        pass

    async with httpx.AsyncClient(base_url="http://opencode",
                                transport=httpx.MockTransport(handle)) as client.http:
        runtime = BucketRuntimeAdapter(config, client, SimpleNamespace(kill=kill))
        runtime.opencode.connected.set()

        async def send(user, chat, request_id):
            submit = Submit(request_id=request_id, chat_id=chat,
                identity=Identity(frontend_id="web", user_id=user), content="hello", deadline_at=time.time() + 30)
            runtime.db.put("run", request_id, {"request_id": request_id, "status": "queued",
                "submit": submit.model_dump(mode="json"), "artifacts": []})
            processing = asyncio.create_task(runtime.process_events())
            try:
                await runtime.run(request_id)
            finally:
                processing.cancel()
                await asyncio.gather(processing, return_exceptions=True)
            row = runtime.db.get("run", request_id)
            assert row["status"] == "completed", json.dumps(row.get("result"))
            return row

        try:
            alice, bob = await asyncio.gather(send("alice", "chat-1", "a1"), send("bob", "chat-1", "b1"))
            again = await send("alice", "chat-1", "a2")
            other = await send("alice", "chat-2", "a3")
            assert alice["session_id"] == again["session_id"]
            assert len({alice["session_id"], bob["session_id"], other["session_id"]}) == 3
            assert alice["directory"] == again["directory"] == other["directory"]
            assert bob["directory"] != alice["directory"]
            assert not client.http.params
            for row in [alice, bob]:
                await runtime.opencode.abort(row["session_id"], directory=Path(row["directory"]))
            assert {directory for _, _, directory in calls} == {alice["directory"], bob["directory"]}
            runtime.db.close()
            runtime = BucketRuntimeAdapter(config, client, SimpleNamespace(kill=kill))
            runtime.opencode.connected.set()
            resumed = await send("alice", "chat-1", "after-restart")
            assert resumed["session_id"] == alice["session_id"]
            assert len(sessions) == 3
            # A session from another location must not be reused in this workspace.
            sessions[alice["session_id"]]["location"]["directory"] = str(config.opencode.context_root)
            stale = Submit.model_validate(alice["submit"]).model_copy(update={"request_id": "stale"})
            runtime.db.put("run", "stale", {"request_id": "stale", "status": "queued",
                "submit": stale.model_dump(mode="json"), "artifacts": []})
            # Session GET is globally addressable; simulate a returned foreign directory.
            previous = client.http

            async def foreign_handle(request):
                if request.url.path == "/api/session/" + alice["session_id"]:
                    return httpx.Response(200, json={"data":sessions[alice["session_id"]]})
                return await handle(request)

            async with httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(foreign_handle)) as foreign:
                client.http = foreign
                await runtime.run("stale")
            client.http = previous
            failed = runtime.db.get("run", "stale")
            assert failed["status"] == "failed"
            assert failed["result"]["error"].startswith("SESSION_WORKSPACE_MISMATCH")
            assert runtime.db.get("session", stale.session_key)["session_id"] == alice["session_id"]
        finally:
            runtime.db.close()


@pytest.mark.asyncio
async def test_recovery_and_cancellation_use_persisted_request_directory(tmp_path):
    config = RuntimeConfig(frontend_id="web", common_root=tmp_path / "common", bucket_id="b", state_root=tmp_path / "state", workspace_root=tmp_path / "users",
        opencode={"context_root": tmp_path / "server"})
    calls = []

    async def handle(request):
        calls.append(request.url.path)
        assert not request.url.params
        return httpx.Response(200, json=[] if request.url.path.endswith("/message") else True)

    async def kill(request_id):
        pass

    client = OpenCodeServer(config, "password")
    await client.http.aclose()
    async with httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(handle)) as client.http:
        runtime = BucketRuntimeAdapter(config, client, SimpleNamespace(kill=kill))
        runtime.opencode.connected.set()
        try:
            for user in ["alice", "bob", "carol"]:
                submit = Submit(request_id=user, chat_id="same", identity=Identity(frontend_id="web", user_id=user),
                                content="hello", deadline_at=time.time() + 30)
                directory = str(runtime.workspace(submit))
                runtime.db.put("run", user, {"request_id": user, "status": "running", "directory": directory,
                    "submit": submit.model_dump(mode="json"), "session_id": user, "artifacts": [],
                    "cancel_requested": user == "carol"})
            await runtime.cancel_request("alice")
            await runtime.recover()
            assert calls == ["/api/session/alice/interrupt", "/api/experimental/session/alice/wait",
                             "/api/session/bob/interrupt", "/api/experimental/session/bob/wait",
                             "/api/session/carol/interrupt", "/api/experimental/session/carol/wait"]
        finally:
            runtime.db.close()
