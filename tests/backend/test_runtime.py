import asyncio
import time

import httpx
import pytest

from agent_backend.config import RuntimeConfig
from agent_backend.models import Envelope, Identity, Submit
from bucket_runtime.opencode_client import OpenCodeClient
from bucket_runtime.runtime import Runtime


class Client:
    def __init__(self):
        self.aborted = []

    async def paths(self):
        return {"worktree": self.worktree}

    async def abort(self, session_id):
        self.aborted.append(session_id)


class Executor:
    def __init__(self):
        self.executions = []

    async def execute(self, request_id, workspace, public, command, workdir, timeout, **options):
        self.executions.append((request_id, command))
        self.last_options = {"public": public, **options}
        return {"output": "ok", "exit_code": 0}

    async def kill(self, request_id):
        pass


@pytest.fixture
def runtime(tmp_path):
    config = RuntimeConfig.model_validate({
        "bucket_id": "b", "control_url": "ws://localhost:8000/internal/buckets/ws",
        "state_root": str(tmp_path / "state"),
        "workspace_root": str(tmp_path / "workspaces"),
        "opencode": {"binary": str(tmp_path / "binary"),
                     "context_root": str(tmp_path / "context"), "config_file": str(tmp_path / "model.json"),
                     "plugin_file": str(tmp_path / "plugin"), "dependency_dir": str(tmp_path / "deps")},
        "profiles": {"p": {"model": "test/test"}},
    })
    instance = Runtime(config, Client(), Executor())
    instance.client.worktree = str(tmp_path)
    yield instance
    instance.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload, expected", [
    ({"healthy": True, "version": "0.0.0-custom"}, True),
    ({"healthy": True}, True),
    ({"healthy": False, "version": "1.18.31"}, False),
])
async def test_opencode_health_does_not_require_a_version(runtime, payload, expected):
    client = OpenCodeClient(runtime.config.opencode, "test-password")
    await client.http.aclose()
    async with httpx.AsyncClient(
        base_url="http://opencode.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    ) as client.http:
        assert await client.health() is expected


def active(runtime, name="alice", request_id="r", session_id="s"):
    submit = Submit(request_id=request_id, conversation_id="conversation-" + name,
        identity=Identity(frontend_id="web", user_id=name), profile_id="p", content="hello",
        deadline_at=time.time() + 60)
    runtime.db.put("run", request_id, {"request_id": request_id, "status": "running",
        "session_id": session_id, "submit": submit.model_dump(mode="json"), "artifacts": []})
    return submit


@pytest.mark.asyncio
async def test_every_request_recreates_deleted_workspace_with_templates(runtime, tmp_path):
    import shutil

    runtime.config.workspace_layout = "frontend_user"
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "AGENTS.md").write_text("workspace instructions")
    runtime.config.workspace_template_root = templates
    workspace = runtime.config.workspace_root / "web" / "alice"
    # Existing user mapping/session is irrelevant to the per-request directory check.
    for index in range(2):
        submit = Submit(request_id=f"check-{index}", conversation_id="chat",
                        identity=Identity(frontend_id="web", user_id="alice"), profile_id="p",
                        content="/cancel", deadline_at=time.time() + 60)
        await runtime.on_command(Envelope(type="request.submit", bucket_id="b",
            request_id=submit.request_id, payload=submit.model_dump(mode="json")))
        assert (workspace / "output").is_dir()
        assert (workspace / "scratch").is_dir()
        assert (workspace / "AGENTS.md").read_text() == "workspace instructions"
        assert runtime.db.get("run", submit.request_id)["status"] == "completed"
        shutil.rmtree(workspace)


def test_workspace_checks_preserve_existing_files_and_repair_subdirectories(runtime, tmp_path):
    import shutil

    submit = active(runtime)
    workspace = runtime.workspace(submit)
    (workspace / "custom.txt").write_text("keep")
    shutil.rmtree(workspace / "output")
    assert runtime.workspace(submit) == workspace
    assert (workspace / "output").is_dir()
    assert (workspace / "custom.txt").read_text() == "keep"


@pytest.mark.asyncio
async def test_workspace_file_in_place_of_directory_fails_request(runtime):
    submit = Submit(request_id="bad-dir", conversation_id="chat",
                    identity=Identity(frontend_id="web", user_id="alice"), profile_id="p",
                    content="hello", deadline_at=time.time() + 60)
    path = runtime.config.workspace_root / submit.identity.frontend_id / submit.identity.key
    path.parent.mkdir(parents=True)
    path.write_text("not a directory")
    await runtime.on_command(Envelope(type="request.submit", bucket_id="b", request_id=submit.request_id,
                                     payload=submit.model_dump(mode="json")))
    assert runtime.db.get("run", submit.request_id)["result"]["error"] == "WORKSPACE_UNAVAILABLE"
    assert path.read_text() == "not a directory"


def test_mount_policy_rejects_host_proc_and_other_user_roots(runtime):
    from pydantic import ValidationError
    for root in ["/proc", str(runtime.config.workspace_root), str(runtime.config.state_root)]:
        config = runtime.config.model_dump(mode="json")
        config["profiles"]["p"]["public_roots"] = [root]
        with pytest.raises(ValidationError):
            RuntimeConfig.model_validate(config)


@pytest.mark.asyncio
async def test_ticket_bound_to_call_and_request_exactly_once(runtime):
    active(runtime)
    args = {"command": "echo hello"}
    response = runtime.authorize("s", "c", "bash", args)
    assert runtime.authorize("s", "c", "bash", args) == response
    with pytest.raises(PermissionError):
        runtime.authorize("s", "c", "bash", {"command": "different"})
    with pytest.raises(PermissionError):
        runtime.authorize("unknown", "c", "bash", args)
    with pytest.raises(PermissionError):
        runtime.authorize("s", "c2", "read", {"filePath": "/etc/passwd"})
    first = await runtime.execute(response["ticket"])
    assert await runtime.execute(response["ticket"]) == first
    assert runtime.executor.executions == [("r", "echo hello")]
    with pytest.raises(PermissionError):
        await runtime.execute("forged")
    row = runtime.db.get("run", "r")
    runtime.db.put("run", "r", {**row, "status": "completed"})
    active(runtime, request_id="r2")
    with pytest.raises(PermissionError):
        await runtime.execute(response["ticket"])


@pytest.mark.asyncio
async def test_publication_rejects_other_user_and_symlink(runtime):
    alice = active(runtime)
    bob = active(runtime, "bob", "r-bob", "s-bob")
    bob_file = runtime.workspace(bob) / "output/report.txt"
    bob_file.write_text("private")
    ticket = runtime.authorize("s", "c", "publish_attachment", {"files": [{"path": str(bob_file)}]})
    with pytest.raises(PermissionError):
        await runtime.publish(ticket["ticket"])
    assert runtime.db.get("run", "r")["artifacts"] == []
    own = runtime.workspace(alice) / "output/link"
    try:
        own.symlink_to(bob_file)
    except OSError:
        return
    ticket = runtime.authorize("s", "c2", "publish_attachment", {"files": [{"path": str(own)}]})
    with pytest.raises(PermissionError):
        await runtime.publish(ticket["ticket"])


@pytest.mark.asyncio
async def test_cancel_before_task_first_step_has_terminal(runtime):
    submit = Submit(request_id="queued", conversation_id="q", identity=Identity(frontend_id="f", user_id="u"),
                    profile_id="p", content="hello", deadline_at=time.time() + 60)
    event = Envelope(type="request.submit", bucket_id="b", request_id="queued", payload=submit.model_dump(mode="json"))
    await runtime.on_command(event)
    await runtime.on_command(Envelope(type="request.cancel", bucket_id="b", request_id="queued"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert runtime.db.get("run", "queued")["status"] == "cancelled"
    await runtime.on_command(event)
    assert "queued" not in runtime.tasks


@pytest.mark.asyncio
async def test_terminal_is_immutable(runtime):
    from agent_backend.models import Result
    active(runtime)
    runtime.finish(Result(request_id="r", status="completed", content="done"))
    runtime.finish(Result(request_id="r", status="cancelled"))
    assert runtime.db.get("run", "r")["result"]["content"] == "done"
    assert len(runtime.db.pending("b")) == 1


@pytest.mark.asyncio
async def test_slash_cancel_scoped_to_user_conversation_and_idempotent(runtime):
    alice = active(runtime)
    active(runtime, "bob", "bob-run", "bob-session")
    other = alice.model_copy(update={"request_id": "other-run", "conversation_id": "other-chat"})
    runtime.db.put("run", "other-run", {"request_id": "other-run", "status": "queued",
                   "submit": other.model_dump(mode="json"), "artifacts": []})
    cancel = alice.model_copy(update={"request_id": "cancel", "content": " /cancel\n"})
    event = Envelope(type="request.submit", bucket_id="b", request_id="cancel",
                     payload=cancel.model_dump(mode="json"))
    await runtime.on_command(event)
    assert runtime.client.aborted == ["s"]
    assert runtime.db.get("run", "r")["status"] == "cancelled"
    assert runtime.db.get("run", "bob-run")["status"] == "running"
    assert runtime.db.get("run", "other-run")["status"] == "queued"
    assert runtime.db.get("run", "cancel")["status"] == "completed"
    assert "cancel" not in runtime.tasks
    await runtime.on_command(event)
    assert runtime.client.aborted == ["s"]


@pytest.mark.asyncio
async def test_slash_cancel_idle_does_not_start_model(runtime):
    submit = active(runtime)
    runtime.db.put("run", "r", {**runtime.db.get("run", "r"), "status": "completed"})
    cancel = submit.model_copy(update={"request_id": "cancel", "content": "/cancel"})
    await runtime.on_command(Envelope(type="request.submit", bucket_id="b", request_id="cancel",
                                     payload=cancel.model_dump(mode="json")))
    assert runtime.db.get("run", "cancel")["status"] == "completed"
    assert runtime.client.aborted == []
    assert not runtime.tasks


def test_permission_file_overrides_are_bound_to_frontend_and_user(runtime, tmp_path):
    from agent_backend.config import PermissionConfig, load_config
    path = tmp_path / "permissions.yaml"
    path.write_text('defaults: {session: {default_tools: true}}\n'
                    'users: [{frontend_id: web, user_id: alice, '
                    'session: {tools: {edit: false}}}]')
    runtime.permissions = load_config(path, PermissionConfig)
    alice = active(runtime)
    assert runtime.policy(alice).session.tools["edit"] is False
    bob = active(runtime, "bob", "bob", "bob")
    assert runtime.policy(bob).session.tools == {}
    other = alice.model_copy(update={"identity": Identity(frontend_id="im", user_id="alice")})
    assert runtime.policy(other) == runtime.policy(bob)


@pytest.mark.asyncio
async def test_supervisor_preserves_server_features(runtime, monkeypatch):
    import json

    from bucket_runtime.supervisor import Supervisor
    oc = runtime.config.opencode
    oc.binary.write_text("#!/bin/sh\nexit 0\n")
    oc.binary.chmod(0o555)
    features = {"permission": {"*": "allow"}, "lsp": {}, "formatter": {},
                "mcp": {"test": {"type": "local", "command": ["test"]}},
                "snapshot": True, "autoupdate": True, "compaction": {"auto": True},
                "plugin": ["custom-plugin"]}
    oc.config_file.write_text(json.dumps(features))
    oc.plugin_file.write_text("plugin")
    oc.dependency_dir.mkdir()
    for name in ["package.json", "package-lock.json"]:
        (oc.dependency_dir / name).write_text("{}")
    package = oc.dependency_dir / "node_modules/@opencode-ai/plugin/package.json"
    package.parent.mkdir(parents=True)
    package.write_text("{}")
    modules = runtime.config.state_root / "opencode-home/config/opencode/node_modules"
    modules.mkdir(parents=True)
    captured = {}

    async def spawn(*args, **kwargs):
        captured.update(kwargs)
        return type("Process", (), {"returncode": None})()

    async def healthy():
        return True

    supervisor = Supervisor(runtime.config)
    monkeypatch.setattr("bucket_runtime.supervisor.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr(supervisor.client, "health", healthy)
    try:
        await supervisor.start()
        env = captured["env"]
        raw = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        for key, value in features.items():
            if key != "plugin":
                assert raw[key] == value
        assert raw["plugin"] == ["custom-plugin", oc.plugin_file.resolve().as_uri()]
        assert not any("DISABLE" in key for key in env)
    finally:
        await supervisor.client.close()


@pytest.mark.asyncio
async def test_slash_cancel_running_task_calls_abort(runtime):
    started = asyncio.Event()

    async def create_session(permissions):
        return "live-session"

    async def submit_message(*args):
        started.set()
        await asyncio.Event().wait()

    runtime.client.create_session = create_session
    runtime.client.submit = submit_message
    submit = Submit(request_id="live", conversation_id="chat", identity=Identity(frontend_id="f", user_id="u"),
                    profile_id="p", content="hello", deadline_at=time.time() + 60)
    await runtime.on_command(Envelope(type="request.submit", bucket_id="b", request_id="live",
                                     payload=submit.model_dump(mode="json")))
    await asyncio.wait_for(started.wait(), 2)
    cancel = submit.model_copy(update={"request_id": "stop", "content": "/cancel"})
    await runtime.on_command(Envelope(type="request.submit", bucket_id="b", request_id="stop",
                                     payload=cancel.model_dump(mode="json")))
    assert runtime.client.aborted == ["live-session"]
    assert runtime.db.get("run", "live")["status"] == "cancelled"
    assert runtime.db.get("run", "stop")["status"] == "completed"


@pytest.mark.asyncio
async def test_child_tool_session_resolves_trusted_parent_and_expires(runtime):
    active(runtime)

    async def session(session_id):
        return {"id": session_id, "parentID": "s"} if session_id == "child" else {"id": session_id}

    runtime.client.session = session
    await runtime.bind_tool_session("child")
    ticket = runtime.authorize("child", "call", "bash", {"command": "echo hi"})
    await runtime.execute(ticket["ticket"])
    assert runtime.executor.executions == [("r", "echo hi")]
    with pytest.raises(PermissionError):
        await runtime.bind_tool_session("unrelated")
    runtime.db.put("run", "r", {**runtime.db.get("run", "r"), "status": "completed"})
    with pytest.raises(PermissionError):
        await runtime.bind_tool_session("child")


@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox_enabled, session_enabled", [(True, True), (True, False), (False, True), (False, False)])
async def test_user_permission_switches_are_independent(runtime, sandbox_enabled, session_enabled):
    from agent_backend.config import PermissionConfig
    runtime.permissions = PermissionConfig.model_validate({"defaults": {
        "sandbox": {"enabled": sandbox_enabled}, "session": {"enabled": session_enabled},
        "filesystem": {"workspace": "read_only", "public_roots": []}}})
    submit = active(runtime)
    assert bool(runtime.session_permissions(submit)) is session_enabled
    ticket = runtime.authorize("s", "cmd", "bash", {"command": "pwd"})
    await runtime.execute(ticket["ticket"])
    assert runtime.executor.last_options["sandbox"].enabled is sandbox_enabled
    assert runtime.executor.last_options["workspace_access"] == "read_only"
    assert runtime.executor.last_options["public"] == []


def test_user_scope_compiles_session_paths_and_checks_native_tools(runtime, tmp_path):
    import fnmatch
    import os

    from agent_backend.config import PermissionConfig
    public = tmp_path / "manuals"
    public.mkdir()
    runtime.permissions = PermissionConfig.model_validate({"defaults": {
        "filesystem": {"public_roots": [str(public)]}, "session": {"tools": {"webfetch": False}}},
        "users": [{"frontend_id": "web", "user_id": "bob", "filesystem": {"workspace": "read_only", "public_roots": []}}]})
    alice = active(runtime)
    bob = active(runtime, "bob", "bob", "bob")
    workspace = runtime.workspace(alice)
    bob_workspace = runtime.workspace(bob)
    rules = runtime.session_permissions(alice)

    def allowed(name, path):
        pattern = os.path.relpath(path, runtime.opencode_worktree).replace("\\", "/")
        matching = [r for r in rules if fnmatch.fnmatchcase(name, r["permission"])
                    and fnmatch.fnmatchcase(pattern, r["pattern"])]
        return matching[-1]["action"] == "allow"

    assert allowed("read", workspace / "file.txt")
    assert allowed("edit", workspace / "file.txt")
    assert allowed("read", public / "SKILL.md")
    assert not allowed("edit", public / "SKILL.md")
    assert not allowed("read", bob_workspace / "file.txt")
    assert not allowed("read", tmp_path / "secret")
    normalized = runtime.authorize("s", "read", "read", {"filePath": "file.txt"})
    assert normalized["args"]["filePath"] == str(workspace / "file.txt")
    for tool, args in [("read", {"filePath": str(bob_workspace / "file.txt")}),
                       ("write", {"filePath": str(public / "SKILL.md")}),
                       ("glob", {"path": str(tmp_path), "pattern": "*"}),
                       ("webfetch", {"url": "https://example.com"})]:
        with pytest.raises(PermissionError):
            runtime.authorize("s", tool, tool, args)
    with pytest.raises(PermissionError):
        runtime.authorize("bob", "edit", "edit", {"filePath": str(bob_workspace / "file.txt")})
    with pytest.raises(PermissionError):
        runtime.authorize("bob", "read", "read", {"filePath": str(public / "SKILL.md")})


def test_permission_scope_rejects_exposed_service_or_user_directories(runtime):
    from agent_backend.config import UserPolicy
    from bucket_runtime.permissions import public_roots
    for root in [runtime.config.workspace_root, runtime.config.state_root / "private", runtime.config.opencode.config_file]:
        policy = UserPolicy.model_validate({"filesystem": {"public_roots": [str(root)]}})
        with pytest.raises(ValueError):
            public_roots(runtime.config, policy, "p")


def test_user_scope_blocks_native_symlinks_and_patch_moves(runtime, tmp_path):
    alice = active(runtime)
    own = runtime.workspace(alice)
    with pytest.raises(PermissionError):
        runtime.authorize("s", "patch", "apply_patch", {"patchText":
            "*** Begin Patch\n*** Update File: output/a\n*** Move to: ../escape\n*** End Patch\n"})
    link = own / "output/link"
    try:
        link.symlink_to(tmp_path / "private")
    except OSError:
        pytest.skip("symlink privileges unavailable")
    with pytest.raises(PermissionError):
        runtime.authorize("s", "read", "read", {"filePath": str(link)})



def test_individual_write_switch_enables_shared_edit_permission(runtime):
    from agent_backend.config import PermissionConfig
    runtime.permissions = PermissionConfig.model_validate({"defaults": {
        "session": {"default_tools": False, "tools": {"write": True}}}})
    submit = active(runtime)
    rules = runtime.session_permissions(submit)
    assert any(rule["permission"] == "edit" and rule["action"] == "allow" for rule in rules)
    assert runtime.authorize("s", "write", "write", {"filePath": "output/new.txt"})["args"]["filePath"]
    with pytest.raises(PermissionError):
        runtime.authorize("s", "edit", "edit", {"filePath": "output/new.txt"})


@pytest.mark.asyncio
async def test_publish_tool_uploads_to_storage_without_control_peer(runtime, tmp_path, monkeypatch):
    from agent_backend.config import StorageConfig
    submit = active(runtime)
    own = runtime.workspace(submit)
    source = own / "output/report.txt"
    source.write_bytes(b"report bytes")
    runtime.config.storage = StorageConfig(endpoint="http://minio:9000", bucket="reports",
        access_key_file=tmp_path / "access", secret_key_file=tmp_path / "secret",
        public_base_url="https://objects.example/reports")
    uploaded = []

    class Storage:
        def upload_file(self, path, bucket, key, ExtraArgs):
            from pathlib import Path
            uploaded.append((Path(path).read_bytes(), bucket, key))

    monkeypatch.setattr(runtime.artifacts, "client", lambda: Storage())
    ticket = runtime.authorize("s", "publish", "publish_attachment", {"files": [{"path": "output/report.txt"}]})
    result = await runtime.publish(ticket["ticket"])
    assert runtime.peer is None
    assert uploaded[0][0] == b"report bytes"
    assert uploaded[0][1] == "reports"
    attachment = result["attachments"][0]
    assert attachment["url"].startswith("https://objects.example/reports/")
    assert submit.identity.key in uploaded[0][2]
    assert runtime.db.get("run", "r")["artifacts"] == result["attachments"]
    assert await runtime.publish(ticket["ticket"]) == result
    assert len(uploaded) == 1


@pytest.mark.asyncio
async def test_failed_minio_upload_does_not_publish_a_url(runtime, tmp_path, monkeypatch):
    from agent_backend.config import StorageConfig
    submit = active(runtime)
    (runtime.workspace(submit) / "output/report.txt").write_text("report")
    runtime.config.storage = StorageConfig(endpoint="http://minio:9000", bucket="reports",
        access_key_file=tmp_path / "access", secret_key_file=tmp_path / "secret")

    class Storage:
        def upload_file(self, *args, **kwargs):
            raise ConnectionError("MinIO unavailable")

    monkeypatch.setattr(runtime.artifacts, "client", lambda: Storage())
    ticket = runtime.authorize("s", "publish", "publish_attachment", {"files": [{"path": "output/report.txt"}]})
    with pytest.raises(ConnectionError):
        await runtime.publish(ticket["ticket"])
    assert runtime.db.get("run", "r")["artifacts"] == []
