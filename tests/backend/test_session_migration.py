import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from support import register_bucket

from agent_backend.config import ControlConfig, RuntimeConfig
from agent_backend.ingress import Inbound
from agent_backend.journal import Journal
from agent_backend.models import Envelope, Identity, Submit
from agent_backend.session_history import transfer_path
from bucket_runtime.adapter import BucketRuntimeAdapter
from bucket_runtime.opencode_server import OpenCodeServer
from container_up.server import ControlService
from container_up.session_manager import SessionManager


class NativeStore:
    """Native HTTP contract, independent stores; no private SQLite fixtures."""
    def __init__(self):
        self.sessions = {}
        self.active = {}
        self.inbox = {}
        self.jobs = []
        self.calls = []
        self.fail_import = False
        self.fail_delete = False
        self.lose_import_reply = False
        self.lose_delete_reply = False

    def handle(self, request):
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        if path == "/api/session/active":
            return httpx.Response(200, json={"data": self.active})
        if path == "/api/experimental/job":
            assert request.url.params["descendants"] == "true"
            return httpx.Response(200, json={"data": self.jobs})
        if path == "/api/session":
            return httpx.Response(200, json={"data": [d["info"] for d in self.sessions.values()
                if d["info"].get("parentID") == request.url.params["parentID"]], "cursor": {}})
        if path.endswith("/import"):
            if self.fail_import:
                return httpx.Response(503)
            data = json.loads(request.content)
            sid = data["info"]["id"]
            if sid in self.sessions:
                return httpx.Response(409)
            if parent := data["info"].get("parentID"):
                assert parent in self.sessions
            data["info"]["location"] = data.pop("location")
            data["info"]["slug"] = "regenerated"
            self.sessions[sid] = data
            if self.lose_import_reply:
                self.lose_import_reply = False
                raise httpx.ReadError("lost import reply", request=request)
            return httpx.Response(200, json={"data": data["info"]})
        sid = path.split("/")[-2] if path.endswith(("/export", "/inbox")) else path.split("/")[-1]
        if sid not in self.sessions:
            return httpx.Response(404)
        if path.endswith("/inbox"):
            return httpx.Response(200, json={"data": self.inbox.get(sid, [])})
        if path.endswith("/export"):
            return httpx.Response(200, json={"data": self.sessions[sid]})
        if method == "DELETE":
            if self.fail_delete:
                return httpx.Response(503)
            ids = {sid}
            for data in self.sessions.values():
                if data["info"].get("parentID") in ids:
                    ids.add(data["info"]["id"])
            for item in ids:
                self.sessions.pop(item)
            if self.lose_delete_reply:
                self.lose_delete_reply = False
                raise httpx.ReadError("lost delete reply", request=request)
            return httpx.Response(204)
        return httpx.Response(200, json={"data": self.sessions[sid]["info"]})


@pytest.fixture
async def migration(tmp_path):
    service = ControlService(ControlConfig(state_root=tmp_path / "control",
                                          frontends={"web": {"provider": "web"}}))
    runtimes, stores = {}, {}
    identity = Identity(frontend_id="web", user_id="alice")
    submit = Submit(request_id="next", identity=identity, chat_id="chat", content="continue",
                    deadline_at=time.time() + 60)
    (tmp_path / "common/web/app").mkdir(parents=True)
    for bucket in ["a", "b"]:
        register_bucket(service, bucket)
        service.config.runtime_templates["web"].state_root = tmp_path / "runtime"
        service.config.runtime_templates["web"].workspace_root = tmp_path / "users"
        config = RuntimeConfig(frontend_id="web", bucket_id=bucket,
            state_root=tmp_path / "runtime/web" / bucket, workspace_root=tmp_path / "users",
            common_root=tmp_path / "common")
        client = OpenCodeServer(config)
        await client.http.aclose()
        stores[bucket] = NativeStore()
        client.http = httpx.AsyncClient(base_url="http://native", transport=httpx.MockTransport(stores[bucket].handle))
        runtime = runtimes[bucket] = BucketRuntimeAdapter(config, client, SimpleNamespace())
        service.pool.peers[bucket] = SimpleNamespace(closed=False, session_call=runtime.control)
    context = {**identity.model_dump(), "identity_id": identity.identity_id, "chat_id": "chat",
               "schema_version": 1, "bucket_id": "a", "requests": {"old-input": "old-request"},
               "input_message_id": "old-input"}
    for sid, parent in [("ses_root", None), ("ses_child", "ses_root")]:
        stores["a"].sessions[sid] = {"info": {"id": sid, "parentID": parent,
            "location": {"directory": str(runtimes["a"].workspace(submit))},
            "metadata": {"bucket_runtime": context}, "permissions": [], "title": "Conversation"},
            "messages": [{"id": sid + "_message", "type": "assistant", "time": {"completed": 1},
                "content": [{"type": "tool", "id": "call", "output": "kept tool result"}]}]}
    service.sessions.record(submit, "a", "ses_root")
    runtimes["a"].db.put("session", submit.session_key, {"session_id": "ses_root"})
    runtimes["a"].db.put("run", "old", {"request_id": "old", "status": "completed"})
    runtimes["a"].db.enqueue("gateway", Envelope(type="runtime.result", bucket_id="a", request_id="old"))
    for sid in ["ses_root", "ses_child", "unrelated"]:
        runtimes["a"].db.put("native_session", sid, {"session_id": sid})
    service.pool.set_accepting("a", False)
    yield service, submit, runtimes, stores
    for runtime in runtimes.values():
        await runtime.opencode.http.aclose()
        runtime.db.close()
    service.db.close()


async def place(service, request_id="next"):
    return await service.submit("web", Inbound(user_id="alice", chat_id="chat", content="continue", request_id=request_id))


async def test_native_transfer_preserves_ids_tools_and_deletes_only_source_history(migration):
    service, submit, runtimes, stores = migration
    original = copy.deepcopy(stores["a"].sessions)
    row = await place(service)
    assert row["bucket_id"] == "b" and row["submit"]["session_id"] == "ses_root"
    assert service.sessions.lookup(submit)["bucket_id"] == "b"
    assert not stores["a"].sessions
    for sid in original:
        assert stores["b"].sessions[sid]["messages"] == original[sid]["messages"]
        info = stores["b"].sessions[sid]["info"]
        context = info["metadata"]["bucket_runtime"]
        assert context["bucket_id"] == "b" and context["requests"] == {}
        assert "input_message_id" not in context
        assert info["permissions"] == runtimes["b"].session_permissions(submit)
        assert runtimes["a"].db.get("native_session", sid) is None
    assert runtimes["a"].db.get("session", submit.session_key) is None
    assert runtimes["a"].db.get("native_session", "unrelated")
    assert runtimes["a"].db.get("run", "old")
    assert runtimes["a"].db.pending("gateway")
    assert not service.db.records("migration")
    assert not transfer_path(runtimes["a"].config.state_root, "a", submit.session_key).exists()
    assert not any("interrupt" in p or p.endswith("/wait") for _, p in stores["a"].calls)


@pytest.mark.parametrize("pending", ["active", "inbox", "child_inbox", "jobs"])
async def test_pending_native_work_keeps_request_on_source_without_export(migration, pending):
    service, _, _, stores = migration
    if pending == "active":
        stores["a"].active["ses_child"] = {"type": "running"}
    elif pending == "jobs":
        stores["a"].jobs = [{"status": "completed"}]
    else:
        stores["a"].inbox["ses_child" if pending == "child_inbox" else "ses_root"] = [{"type": "user"}]
    row = await place(service)
    assert row["bucket_id"] == "a"
    assert not stores["b"].sessions
    assert not any(p.endswith("/export") or m == "DELETE" for m, p in stores["a"].calls)
    assert not service.db.records("migration")


@pytest.mark.parametrize("failure", ["fail_import", "lose_import_reply", "fail_delete", "lose_delete_reply"])
async def test_failed_transfer_retries_after_manager_restart_before_submitting(migration, failure):
    service, submit, runtimes, stores = migration
    failing = stores["b"] if "import" in failure else stores["a"]
    setattr(failing, failure, True)
    with pytest.raises(httpx.HTTPError):
        await place(service)
    assert service.sessions.lookup(submit)["bucket_id"] == "a"
    assert service.db.get("request", "next") is None
    assert service.db.records("migration")
    assert service.pool.busy("a") and service.pool.busy("b")
    assert transfer_path(runtimes["a"].config.state_root, "a", submit.session_key).exists()
    if failure != "lose_delete_reply":
        assert "ses_root" in stores["a"].sessions
    setattr(failing, failure, False)
    service.db.close()
    service.db = Journal(service.config.state_root / "control.sqlite")
    service.pool.db = service.db
    service.sessions = SessionManager(service.config, service.db, service.open_session_source)
    assert (await place(service))["bucket_id"] == "b"
    assert not stores["a"].sessions
    assert not service.db.records("migration")


async def test_new_chat_cannot_bypass_failed_migration(migration):
    service, _, _, stores = migration
    stores["a"].fail_delete = True
    with pytest.raises(httpx.HTTPError):
        await place(service)
    with pytest.raises(httpx.HTTPError):
        await service.submit("web", Inbound(user_id="alice", chat_id="other", content="new", request_id="other"))
    assert service.db.get("request", "other") is None
    stores["a"].fail_delete = False
    row = await service.submit("web", Inbound(user_id="alice", chat_id="other", content="new", request_id="other"))
    assert row["bucket_id"] == "b" and row["submit"]["session_id"] is None
    assert not stores["a"].sessions


async def test_busy_target_rejects_control_without_waiting_on_execution_lock(migration):
    service, submit, runtimes, stores = migration
    runtime = runtimes["b"]
    runtime.db.put("run", "active", {"request_id": "active", "status": "running",
        "submit": submit.model_dump(mode="json")})
    lock = runtime.request_locks.setdefault(submit.identity.identity_id, asyncio.Lock())
    async with lock:
        with pytest.raises(RuntimeError, match="SESSION_BUSY"):
            await asyncio.wait_for(place(service), 1)
    assert "ses_root" in stores["a"].sessions
    assert service.db.get("request", "next") is None


async def test_target_pending_work_or_source_change_prevents_delete(migration):
    service, _, _, stores = migration
    stores["b"].inbox["ses_root"] = [{"type": "user"}]
    with pytest.raises(RuntimeError, match="pending work"):
        await place(service)
    assert "ses_root" in stores["a"].sessions
    stores["b"].inbox.clear()
    stores["a"].sessions["ses_root"]["messages"].append({"id": "late", "type": "user"})
    with pytest.raises(RuntimeError, match="readback mismatch"):
        await place(service)
    assert "ses_root" in stores["a"].sessions
    assert service.db.get("request", "next") is None


@pytest.mark.parametrize("conflict", ["owner", "history"])
async def test_import_conflict_requires_same_owner_and_exact_history(migration, conflict):
    service, submit, _, stores = migration
    stores["b"].lose_import_reply = True
    with pytest.raises(httpx.ReadError):
        await place(service)
    root = stores["b"].sessions["ses_root"]
    if conflict == "owner":
        root["info"]["metadata"]["bucket_runtime"]["user_id"] = "someone-else"
    else:
        root["messages"][0]["content"] = []
    with pytest.raises((PermissionError, RuntimeError)):
        await place(service)
    assert "ses_root" in stores["a"].sessions
    assert service.sessions.lookup(submit)["bucket_id"] == "a"
    assert service.db.get("request", "next") is None


@pytest.mark.parametrize("status", ["idle", "missing"])
async def test_offline_source_starts_native_service_on_original_bucket(migration, status):
    service, _, runtimes, stores = migration
    service.pool.peers.pop("a")
    service.db.put("bucket", "a", {**service.db.get("bucket", "a"), "status": status})
    async def ensure(bucket_id, frontend_id, *, allow_create):
        assert bucket_id == "a" and frontend_id == "web"
        assert allow_create == (status == "missing")
        service.pool.peers["a"] = SimpleNamespace(closed=False, session_call=runtimes["a"].control)
    service.pool.manager.ensure = AsyncMock(side_effect=ensure)
    assert (await place(service))["bucket_id"] == "b"
    service.pool.manager.ensure.assert_awaited_once()
    assert not stores["a"].sessions


async def test_late_started_event_cannot_move_catalog_back(migration):
    service, submit, _, _ = migration
    row = await place(service)
    await service.on_event("b", Envelope(type="request.completed", bucket_id="b", request_id=row["request_id"],
        payload={"request_id": row["request_id"], "status": "completed"}))
    service.sessions.record(submit, "a", "ses_root")
    await service.on_event("b", Envelope(type="request.started", bucket_id="b", request_id=row["request_id"],
                                        payload={"session_id": "ses_root"}))
    assert service.sessions.lookup(submit)["bucket_id"] == "a"
