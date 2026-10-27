from support import emit_answer
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agent_backend.config import RuntimeConfig
from agent_backend.models import Identity, Submit
from bucket_runtime.opencode_client import OpenCodeClient
from bucket_runtime.runtime import Runtime


@pytest.mark.asyncio
async def test_concurrent_users_chat_reuse_and_all_requests_keep_their_directory(tmp_path):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "AGENTS.md").write_text("managed rules")
    config = RuntimeConfig(frontend_id="web", common_root=tmp_path / "common", bucket_id="b", state_root=tmp_path / "state",
        workspace_root=tmp_path / "users",
        opencode={"context_root": tmp_path / "server"})
    sessions, messages, calls = {}, {}, []
    both_users = asyncio.Event()
    path_directories = set()

    async def handle(request):
        directory = request.url.params["directory"]
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, path, directory))
        await asyncio.sleep(0)  # Interleave shared-client operations.
        if path == "/config":
            return httpx.Response(200, json={"model": "intranet/current-model"})
        if path == "/path":
            path_directories.add(directory)
            if len(path_directories) == 2:
                both_users.set()
            await asyncio.wait_for(both_users.wait(), 5)
            return httpx.Response(200, json={"worktree": directory})
        if path == "/session":
            assert not (Path(directory) / "AGENTS.md").exists()
            session_id = f"ses_{len(sessions)}"
            info = {"id": session_id, "directory": directory, "permission": body["permission"],
                    "metadata": body["metadata"]}
            sessions[session_id] = info
            messages[session_id] = []
            return httpx.Response(200, json=info)
        session_id = path.split("/")[2]
        info = sessions[session_id]
        assert directory == info["directory"], (path, directory, info)
        if path.endswith("/prompt_async"):
            assert body["model"] == {"providerID": "intranet", "modelID": "current-model"}
            assert "agent" not in body
            messages[session_id].append({"info": {"id": body["messageID"], "role": "user"}})
            messages[session_id].append({"info": {"id": "answer_" + body["messageID"],
                "role": "assistant", "sessionID": session_id, "parentID": body["messageID"], "time": {"completed": 1},
                "finish": "stop"}, "parts": [{"type": "text", "text": "done"}]})
            context = info["metadata"]["bucket_runtime"]
            emit_answer(runtime, session_id, context["requests"][body["messageID"]],
                        body["messageID"], directory, messages[session_id][-1])
            return httpx.Response(200, json={})
        if path.endswith("/message"):
            pytest.fail("reply polling is forbidden")
        if request.method == "PATCH":
            info["metadata"] = body["metadata"]
        return httpx.Response(200, json=info)

    client = OpenCodeClient(config.opencode, "password")
    await client.http.aclose()

    async def kill(request_id):
        pass

    async with httpx.AsyncClient(base_url="http://opencode", params={"directory": str(config.opencode.context_root)},
                                transport=httpx.MockTransport(handle)) as client.http:
        runtime = Runtime(config, client, SimpleNamespace(kill=kill))
        runtime.native_events.connected.set()

        async def send(user, chat, request_id):
            submit = Submit(request_id=request_id, chat_id=chat,
                identity=Identity(frontend_id="web", user_id=user), content="hello", deadline_at=time.time() + 30)
            runtime.db.put("run", request_id, {"request_id": request_id, "status": "queued",
                "submit": submit.model_dump(mode="json"), "artifacts": []})
            await runtime.run(request_id)
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
            assert client.http.params["directory"] == str(config.opencode.context_root)
            assert client.directory == config.opencode.context_root.resolve()
            for row in [alice, bob]:
                await runtime.run_client(row).abort(row["session_id"])
                assert runtime.worktree(Submit.model_validate(row["submit"])) == Path(row["directory"])
            assert {directory for _, _, directory in calls} == {alice["directory"], bob["directory"]}
            runtime.db.close()
            runtime = Runtime(config, client, SimpleNamespace(kill=kill))
            runtime.native_events.connected.set()
            resumed = await send("alice", "chat-1", "after-restart")
            assert resumed["session_id"] == alice["session_id"]
            assert len(sessions) == 3
            # A native session created by the old global-directory implementation
            # must never be silently reused as though it belonged to this workspace.
            sessions[alice["session_id"]]["directory"] = str(config.opencode.context_root)
            stale = Submit.model_validate(alice["submit"]).model_copy(update={"request_id": "stale"})
            runtime.db.put("run", "stale", {"request_id": "stale", "status": "queued",
                "submit": stale.model_dump(mode="json"), "artifacts": []})
            # Session GET is globally addressable; simulate a returned legacy directory.
            previous = client.http

            async def legacy_handle(request):
                if request.url.path == "/session/" + alice["session_id"]:
                    return httpx.Response(200, json=sessions[alice["session_id"]])
                return await handle(request)

            async with httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(legacy_handle)) as legacy:
                client.http = legacy
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
        calls.append((request.url.path, request.url.params["directory"]))
        return httpx.Response(200, json=[] if request.url.path.endswith("/message") else True)

    async def kill(request_id):
        pass

    client = OpenCodeClient(config.opencode, "password")
    await client.http.aclose()
    async with httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(handle)) as client.http:
        runtime = Runtime(config, client, SimpleNamespace(kill=kill))
        runtime.native_events.connected.set()
        try:
            for user in ["alice", "bob"]:
                submit = Submit(request_id=user, chat_id="same", identity=Identity(frontend_id="web", user_id=user),
                                content="hello", deadline_at=time.time() + 30)
                directory = str(runtime.workspace(submit))
                runtime.db.put("run", user, {"request_id": user, "status": "running", "directory": directory,
                    "submit": submit.model_dump(mode="json"), "session_id": user, "artifacts": []})
            await runtime.cancel_request("alice")
            await runtime.recover()
            assert calls == [("/session/alice/abort", runtime.db.get("run", "alice")["directory"]),
                             ("/session/bob/abort", runtime.db.get("run", "bob")["directory"])]
        finally:
            runtime.db.close()
