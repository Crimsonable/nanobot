from tests.backend.support import plugin_scope, publication
import asyncio
import time

import httpx
import pytest

from agent_backend.config import RuntimeConfig
from agent_backend.models import Envelope, Identity, Submit
from bucket_runtime.opencode_client import OpenCodeClient
from bucket_runtime.runtime import Runtime
from bucket_runtime.sandbox_executor import SandboxExecutor


class Client:
    def __init__(self):
        self.aborted = []

    def for_directory(self, directory):
        from copy import copy
        scoped = copy(self)
        scoped.directory = directory
        return scoped

    async def paths(self):
        return {"worktree": self.worktree}

    async def abort(self, session_id):
        self.aborted.append(session_id)


class Executor(SandboxExecutor):
    def __init__(self):
        from agent_backend.config import SandboxConfig
        super().__init__(SandboxConfig(), 4096)


@pytest.fixture
def runtime(tmp_path):
    config = RuntimeConfig.model_validate({
        "bucket_id": "b", "frontend_id": "web", "common_root": str(tmp_path / "common"), "control_url": "ws://localhost:8000/internal/buckets/ws",
        "state_root": str(tmp_path / "state"),
        "workspace_root": str(tmp_path / "workspaces"),
        "opencode": {"binary": str(tmp_path / "binary"),
                     "context_root": str(tmp_path / "context"), "config_file": str(tmp_path / "model.json"),
                     "plugin_file": str(tmp_path / "plugin"), "dependency_dir": str(tmp_path / "deps")},
    })
    instance = Runtime(config, Client(), Executor())
    instance.native_events.connected.set()
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
    submit = Submit(request_id=request_id, chat_id="chat-" + name,
        identity=Identity(frontend_id="web", user_id=name), content="hello",
        deadline_at=time.time() + 60)
    runtime.db.put("run", request_id, {"request_id": request_id, "status": "running",
        "session_id": session_id, "directory": str(runtime.workspace(submit)), "submit": submit.model_dump(mode="json"), "artifacts": []})
    return submit


@pytest.mark.asyncio
async def test_migration_stops_native_session_tree_and_removes_only_local_history(runtime):
    submit = active(runtime)
    payload = {"identity": submit.identity.model_dump(), "chat_id": submit.chat_id, "session_id": "s"}
    with pytest.raises(RuntimeError, match="outstanding"):
        await runtime.session_operation({**payload, "operation": "quiesce"})
    row = runtime.db.get("run", "r")
    runtime.db.put("run", "r", {**row, "status": "completed"})
    operations = []

    async def session(session_id):
        return {"id": session_id, "directory": row["directory"],
                "metadata": {"bucket_runtime": {**submit.identity.model_dump(), "chat_id": submit.chat_id}}}

    async def children(session_id):
        return [{"id": "child"}] if session_id == "s" else []

    async def abort(session_id):
        operations.append(("abort", session_id))

    async def delete(session_id):
        operations.append(("delete", session_id))

    runtime.client.session = session
    runtime.client.children = children
    runtime.client.abort = abort
    runtime.client.delete_session = delete
    runtime.db.put("session", submit.session_key, {"session_id": "s"})
    runtime.db.put("message", "one", {"session_id": "s"})
    runtime.db.put("message", "two", {"session_id": "other"})
    runtime.db.put("child_session", "child", {"request_id": "r"})
    runtime.emit("request.completed", "r", {"content": "audit"})
    await runtime.session_operation({**payload, "operation": "quiesce"})
    await runtime.session_operation({**payload, "operation": "delete", "session_ids": ["s", "child"]})
    assert operations == [("abort", "s"), ("abort", "child"), ("delete", "s")]
    assert runtime.db.get("session", submit.session_key) is None
    assert runtime.db.get("message", "one") is None
    assert runtime.db.get("message", "two")
    assert runtime.db.get("child_session", "child") is None
    assert runtime.db.get("run", "r")
    assert runtime.db.pending("b")


@pytest.mark.asyncio
async def test_every_request_recreates_deleted_workspace_without_copying_templates(runtime, tmp_path):
    import shutil


    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "AGENTS.md").write_text("workspace instructions")
    workspace = runtime.config.workspace_root / "web" / "alice"
    # Existing user mapping/session is irrelevant to the per-request directory check.
    for index in range(2):
        submit = Submit(request_id=f"check-{index}", chat_id="chat",
                        identity=Identity(frontend_id="web", user_id="alice"), content="/cancel", deadline_at=time.time() + 60)
        await runtime.on_command(Envelope(type="request.submit", bucket_id="b",
            request_id=submit.request_id, payload=submit.model_dump(mode="json")))
        assert workspace.is_dir()
        assert list(workspace.iterdir()) == []
        assert not (workspace / "AGENTS.md").exists()
        assert runtime.db.get("run", submit.request_id)["status"] == "completed"
        shutil.rmtree(workspace)


def test_workspace_checks_preserve_existing_files(runtime, tmp_path):
    submit = active(runtime)
    workspace = runtime.workspace(submit)
    (workspace / "custom.txt").write_text("keep")
    (workspace / "AGENTS.md").write_text("user rules")
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "AGENTS.md").write_text("new template rules")
    assert runtime.workspace(submit) == workspace
    assert {p.name for p in workspace.iterdir()} == {"custom.txt", "AGENTS.md"}
    assert (workspace / "custom.txt").read_text() == "keep"
    assert (workspace / "AGENTS.md").read_text() == "user rules"


@pytest.mark.asyncio
async def test_workspace_file_in_place_of_directory_fails_request(runtime):
    submit = Submit(request_id="bad-dir", chat_id="chat",
                    identity=Identity(frontend_id="web", user_id="alice"), content="hello", deadline_at=time.time() + 60)
    path = runtime.config.workspace_root / submit.identity.frontend_id / submit.identity.user_id
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
        config["sandbox"]["readonly_roots"] = [root]
        with pytest.raises(ValidationError):
            RuntimeConfig.model_validate(config)


def test_bash_authorization_returns_scope_without_execution_ticket(runtime):
    submit = active(runtime)
    response = plugin_scope(runtime, "s", "c", "bash", {"command": "echo hello", "timeout": 1234})
    assert response["argv"][-3:] == ["--", "/bin/sh", "-c"]
    assert response["argv"][:3] == ["/usr/bin/flock", "--exclusive", "--close"]
    assert "--clearenv" in response["argv"]
    assert str(runtime.workspace(submit)) in response["argv"]
    assert response["timeout_ms"] == 1234
    assert "ticket" not in response
    assert not runtime.db.records("ticket")
    assert not hasattr(runtime, "execute")
    with pytest.raises(PermissionError):
        plugin_scope(runtime, "unknown", "c", "bash", {"command": "echo hi"})
    with pytest.raises(PermissionError):
        plugin_scope(runtime, "s", "c", "bash", {"command": "pwd", "workdir": "/etc"})
    with pytest.raises(PermissionError):
        plugin_scope(runtime, "s", "c2", "read", {"filePath": "/etc/passwd"})
    for timeout in [-1, 0, "100", True]:
        with pytest.raises(ValueError):
            plugin_scope(runtime, "s", "bad", "bash", {"command": "pwd", "timeout": timeout})


@pytest.mark.asyncio
async def test_authorized_native_shell_isolated_and_releases_lock_on_cancel(runtime, tmp_path):
    import os
    import shlex
    import signal
    from bucket_runtime.file_lock import publication_lock

    submit = active(runtime)
    outside = tmp_path / "secret"
    outside.write_text("private")
    workspace = runtime.workspace(submit)
    command = f"test ! -e {shlex.quote(str(outside))} && touch started; sleep 30; touch survived"
    auth = plugin_scope(runtime, "s", "native", "bash", {"command": command})
    process = await asyncio.create_subprocess_exec("/bin/sh", "-c",
        "exec " + shlex.join([*auth["argv"], command]), start_new_session=True)
    try:
        async with asyncio.timeout(5):
            while not (workspace / "started").exists():
                assert process.returncode is None
                await asyncio.sleep(0.02)
        async with asyncio.timeout(0.1):
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):
                    async with publication_lock(runtime.user_lock_path(submit)):
                        pytest.fail("native command must hold publication lock")
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
    async with asyncio.timeout(5):
        async with publication_lock(runtime.user_lock_path(submit)):
            pass
    assert not (workspace / "survived").exists()


def test_bash_scope_respects_readonly_none_network_and_deadline(runtime):
    submit = active(runtime)
    workspace = str(runtime.workspace(submit))
    policy = runtime.permissions.defaults
    policy.filesystem.workspace = "read_only"
    policy.sandbox.network = False
    authorization = plugin_scope(runtime, "s", "ro", "bash", {"command": "pwd", "timeout": 999999})
    argv = authorization["argv"]
    assert "--bind" not in argv and "--share-net" not in argv
    assert argv[argv.index(workspace) - 1] == "--ro-bind"
    assert 0 < authorization["timeout_ms"] <= 60000
    policy.filesystem.workspace = "none"
    argv = plugin_scope(runtime, "s", "none", "bash", {"command": "pwd"})["argv"]
    assert workspace not in argv
    assert argv[argv.index("--chdir") + 1] == "/"
    with pytest.raises(PermissionError):
        plugin_scope(runtime, "s", "denied-cwd", "bash", {"command": "pwd", "workdir": workspace})
    row = runtime.db.get("run", "r")
    row["submit"]["deadline_at"] = time.time() - 1
    runtime.db.put("run", "r", row)
    with pytest.raises(PermissionError, match="deadline"):
        plugin_scope(runtime, "s", "expired", "bash", {"command": "pwd"})


@pytest.mark.asyncio
async def test_publication_rejects_other_user_and_symlink(runtime):
    alice = active(runtime)
    bob = active(runtime, "bob", "r-bob", "s-bob")
    bob_file = runtime.workspace(bob) / "report.txt"
    bob_file.write_text("private")
    body = publication(runtime, "s", "c", [{"path": str(bob_file)}])
    with pytest.raises(PermissionError):
        await runtime.publish(body)
    assert runtime.db.get("run", "r")["artifacts"] == []
    own = runtime.workspace(alice) / "link"
    try:
        own.symlink_to(bob_file)
    except OSError:
        return
    body = publication(runtime, "s", "c2", [{"path": str(own)}])
    with pytest.raises(PermissionError):
        await runtime.publish(body)


@pytest.mark.asyncio
async def test_cancel_before_task_first_step_has_terminal(runtime):
    submit = Submit(request_id="queued", chat_id="q", identity=Identity(frontend_id="web", user_id="u"),
                    content="hello", deadline_at=time.time() + 60)
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
async def test_slash_cancel_scoped_to_frontend_user_chat_and_idempotent(runtime):
    alice = active(runtime)
    bob = alice.model_copy(update={"request_id": "bob-run", "identity": Identity(frontend_id="web", user_id="bob")})
    runtime.db.put("run", "bob-run", {"request_id": "bob-run", "status": "running",
                   "submit": bob.model_dump(mode="json"), "session_id": "bob-session", "artifacts": []})
    other = alice.model_copy(update={"request_id": "other-run", "chat_id": "other-chat"})
    runtime.db.put("run", "other-run", {"request_id": "other-run", "status": "queued",
                   "submit": other.model_dump(mode="json"), "artifacts": []})
    frontend = alice.model_copy(update={"request_id": "frontend-run", "identity": Identity(frontend_id="im", user_id="alice")})
    runtime.db.put("run", "frontend-run", {"request_id": "frontend-run", "status": "queued",
                   "submit": frontend.model_dump(mode="json"), "artifacts": []})
    cancel = alice.model_copy(update={"request_id": "cancel", "content": " /cancel\n"})
    event = Envelope(type="request.submit", bucket_id="b", request_id="cancel",
                     payload=cancel.model_dump(mode="json"))
    await runtime.on_command(event)
    assert runtime.client.aborted == ["s"]
    assert runtime.db.get("run", "r")["status"] == "cancelled"
    assert runtime.db.get("run", "bob-run")["status"] == "running"
    assert runtime.db.get("run", "other-run")["status"] == "queued"
    assert runtime.db.get("run", "frontend-run")["status"] == "queued"
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
    modules.parent.mkdir(parents=True)
    modules.symlink_to(oc.dependency_dir / "node_modules", target_is_directory=True)
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

    async def create_session(permissions, context):
        assert context["user_id"] == "u"
        assert context["requests"][context["input_message_id"]] == "live"
        return "live-session"

    async def submit_message(*args):
        started.set()
        await asyncio.Event().wait()

    runtime.client.create_session = create_session
    runtime.client.submit = submit_message
    submit = Submit(request_id="live", chat_id="chat", identity=Identity(frontend_id="web", user_id="u"),
                    content="hello", deadline_at=time.time() + 60)
    await runtime.on_command(Envelope(type="request.submit", bucket_id="b", request_id="live",
                                     payload=submit.model_dump(mode="json")))
    await asyncio.wait_for(started.wait(), 2)
    cancel = submit.model_copy(update={"request_id": "stop", "content": "/cancel"})
    await runtime.on_command(Envelope(type="request.submit", bucket_id="b", request_id="stop",
                                     payload=cancel.model_dump(mode="json")))
    assert runtime.client.aborted == ["live-session"]
    assert runtime.db.get("run", "live")["status"] == "cancelled"
    assert runtime.db.get("run", "stop")["status"] == "completed"



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
        pattern = os.path.relpath(path, runtime.worktree(alice)).replace("\\", "/")
        matching = [r for r in rules if fnmatch.fnmatchcase(name, r["permission"])
                    and fnmatch.fnmatchcase(pattern, r["pattern"])]
        return matching[-1]["action"] == "allow"

    assert allowed("read", workspace / "file.txt")
    assert allowed("edit", workspace / "file.txt")
    assert allowed("read", public / "SKILL.md")
    assert not allowed("edit", public / "SKILL.md")
    assert not allowed("read", bob_workspace / "file.txt")
    assert not allowed("read", tmp_path / "secret")
    normalized = plugin_scope(runtime, "s", "read", "read", {"filePath": "file.txt"})
    assert normalized["args"]["filePath"] == str(workspace / "file.txt")
    for tool, args in [("read", {"filePath": str(bob_workspace / "file.txt")}),
                       ("write", {"filePath": str(public / "SKILL.md")}),
                       ("glob", {"path": str(tmp_path), "pattern": "*"}),
                       ("webfetch", {"url": "https://example.com"})]:
        with pytest.raises(PermissionError):
            plugin_scope(runtime, "s", tool, tool, args)
    with pytest.raises(PermissionError):
        plugin_scope(runtime, "bob", "edit", "edit", {"filePath": str(bob_workspace / "file.txt")})
    with pytest.raises(PermissionError):
        plugin_scope(runtime, "bob", "read", "read", {"filePath": str(public / "SKILL.md")})


def test_permission_scope_rejects_exposed_service_or_user_directories(runtime):
    from agent_backend.config import UserPolicy
    from bucket_runtime.permissions import public_roots
    for root in [runtime.config.workspace_root, runtime.config.state_root / "private", runtime.config.opencode.config_file]:
        policy = UserPolicy.model_validate({"filesystem": {"public_roots": [str(root)]}})
        with pytest.raises(ValueError):
            public_roots(runtime.config, policy)


def test_user_scope_blocks_native_symlinks_and_patch_moves(runtime, tmp_path):
    alice = active(runtime)
    own = runtime.workspace(alice)
    with pytest.raises(PermissionError):
        plugin_scope(runtime, "s", "patch", "apply_patch", {"patchText":
            "*** Begin Patch\n*** Update File: a\n*** Move to: ../escape\n*** End Patch\n"})
    link = own / "link"
    try:
        link.symlink_to(tmp_path / "private")
    except OSError:
        pytest.skip("symlink privileges unavailable")
    with pytest.raises(PermissionError):
        plugin_scope(runtime, "s", "read", "read", {"filePath": str(link)})


def test_individual_write_switch_enables_shared_edit_permission(runtime):
    from agent_backend.config import PermissionConfig
    runtime.permissions = PermissionConfig.model_validate({"defaults": {
        "session": {"default_tools": False, "tools": {"write": True}}}})
    submit = active(runtime)
    rules = runtime.session_permissions(submit)
    assert any(rule["permission"] == "edit" and rule["action"] == "allow" for rule in rules)
    assert plugin_scope(runtime, "s", "write", "write", {"filePath": "new.txt"})["args"]["filePath"]
    with pytest.raises(PermissionError):
        plugin_scope(runtime, "s", "edit", "edit", {"filePath": "new.txt"})


@pytest.mark.asyncio
async def test_publish_tool_uploads_to_storage_without_control_peer(runtime, tmp_path, monkeypatch):
    from agent_backend.config import StorageConfig
    submit = active(runtime)
    own = runtime.workspace(submit)
    source = own / "report.txt"
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
    body = publication(runtime, "s", "publish", [{"path": "report.txt"}])
    result = await runtime.publish(body)
    assert runtime.peer is None
    assert uploaded[0][0] == b"report bytes"
    assert uploaded[0][1] == "reports"
    attachment = result["attachments"][0]
    assert attachment["url"].startswith("https://objects.example/reports/")
    assert submit.identity.identity_id in uploaded[0][2]
    assert runtime.db.get("run", "r")["artifacts"] == result["attachments"]
    assert await runtime.publish(body) == result
    assert len(uploaded) == 1


@pytest.mark.asyncio
async def test_failed_minio_upload_does_not_publish_a_url(runtime, tmp_path, monkeypatch):
    from agent_backend.config import StorageConfig
    submit = active(runtime)
    (runtime.workspace(submit) / "report.txt").write_text("report")
    runtime.config.storage = StorageConfig(endpoint="http://minio:9000", bucket="reports",
        access_key_file=tmp_path / "access", secret_key_file=tmp_path / "secret")

    class Storage:
        def upload_file(self, *args, **kwargs):
            raise ConnectionError("MinIO unavailable")

    monkeypatch.setattr(runtime.artifacts, "client", lambda: Storage())
    body = publication(runtime, "s", "publish", [{"path": "report.txt"}])
    with pytest.raises(ConnectionError):
        await runtime.publish(body)
    assert runtime.db.get("run", "r")["artifacts"] == []


def test_bu_returns_complete_permission_scope_without_metadata_copy(runtime):
    submit = active(runtime)
    runtime.permissions.defaults.filesystem.workspace = "read_only"
    runtime.permissions.defaults.sandbox.network = False
    context = runtime.session_context(submit, "input")
    assert context["user_id"] == "alice" and context["chat_id"] == submit.chat_id
    assert "execution" not in context
    execution = runtime.execution_scope(submit)
    assert execution["workspace"] == str(runtime.workspace(submit))
    assert execution["policy"]["filesystem"]["workspace"] == "read_only"
    assert execution["policy"]["sandbox"]["network"] is False
    assert execution["public_roots"] == [str(p) for p in runtime.public_roots(submit)]
    assert execution["private_files"] == [str(p.resolve()) for p in runtime.private_files()]
    assert execution["lock_path"] == str(runtime.user_lock_path(submit))
    assert execution["deadline_at"] == submit.deadline_at
    assert not hasattr(runtime, "authorize")
    assert not hasattr(runtime, "bind_tool_session")


@pytest.mark.asyncio
async def test_session_permission_lookup_and_publication_check_current_request(runtime):
    from bucket_runtime.server import rpc_app
    submit = active(runtime)
    directory = str(runtime.workspace(submit))
    queried = []

    async def session(session_id):
        queried.append(session_id)
        return {"directory": directory, "parentID": "s" if session_id == "child" else None}

    runtime.client.session = session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rpc_app(runtime, "key")),
                                 base_url="http://runtime", headers={"Authorization": "Bearer key"}) as client:
        body = {"session_id": "s", "directory": directory}
        response = await client.post("/permissions", json=body)
        assert response.status_code == 200
        assert response.json()["workspace"] == directory
        assert queried == []  # Root session permission lookup never calls OpenCode.
        response = await client.post("/permissions", json={**body, "session_id": "child"})
        assert response.status_code == 200 and queried == ["child"]
        assert (await client.post("/permissions", json={**body, "directory": "/wrong"})).status_code == 403
        assert (await client.post("/permissions", json={**body, "session_id": "unknown"})).status_code == 403
        runtime.permissions.defaults.session.tools["publish_attachment"] = False
        response = await client.post("/publish", json={**body, "message_id": "m", "files": [{"path": "x"}]})
        assert response.status_code == 400 and "disabled" in response.text
        runtime.db.put("run", "r", {**runtime.db.get("run", "r"), "cancel_requested": True})
        assert (await client.post("/permissions", json=body)).status_code == 403
        assert (await client.post("/publish", json={**body, "message_id": "m", "files": [{"path": "x"}]})).status_code == 400
        assert not runtime.db.records("ticket") and not runtime.db.records("child_session")
