import asyncio
import time

import httpx
import pytest

from agent_backend.config import RuntimeConfig
from agent_backend.models import Envelope, Identity, Submit
from bucket_runtime.adapter import BucketRuntimeAdapter
from bucket_runtime.opencode_server import OpenCodeServer
from bucket_runtime.sandbox_executor import SandboxExecutor
from tests.backend.support import native, plugin_scope, publication


class Client:
    def __init__(self):
        self.aborted = []
        self.connected = asyncio.Event()

    async def abort(self, session_id, *, directory=None):
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
                     "plugin_dir": str(tmp_path / "plugin")},
    })
    instance = BucketRuntimeAdapter(config, Client(), Executor())
    instance.opencode.connected.set()
    yield instance
    instance.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["2.3.2", "2.4.0", "1.18.31", "3.0.0"])
async def test_opencode_health_requires_v2(runtime, version):
    client = OpenCodeServer(runtime.config, "test-password")
    await client.http.aclose()
    def handle(request):
        assert request.url.path == "/api/info"
        return httpx.Response(200, json={"version": version})
    async with httpx.AsyncClient(base_url="http://opencode.test", transport=httpx.MockTransport(handle)) as client.http:
        if version.startswith("2."):
            assert await client.health()
        else:
            with pytest.raises(RuntimeError, match="2.x"):
                await client.health()


def active(runtime, name="alice", request_id="r", session_id="s"):
    submit = Submit(request_id=request_id, chat_id="chat-" + name,
        identity=Identity(frontend_id="web", user_id=name), content="hello",
        deadline_at=time.time() + 60)
    runtime.db.put("run", request_id, {"request_id": request_id, "status": "running",
        "session_id": session_id, "directory": str(runtime.workspace(submit)), "submit": submit.model_dump(mode="json"), "artifacts": []})
    return submit


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
    response = plugin_scope(runtime, "s", "c", "shell", {"command": "echo hello", "timeout": 1234})
    assert response["argv"][-3:] == ["--", "/bin/sh", "-c"]
    assert response["argv"][:3] == ["/usr/bin/flock", "--exclusive", "--close"]
    assert "--clearenv" in response["argv"]
    assert str(runtime.workspace(submit)) in response["argv"]
    assert response["timeout_ms"] == 1234
    assert "ticket" not in response
    assert not runtime.db.records("ticket")
    assert not hasattr(runtime, "execute")
    with pytest.raises(PermissionError):
        plugin_scope(runtime, "unknown", "c", "shell", {"command": "echo hi"})
    with pytest.raises(PermissionError):
        plugin_scope(runtime, "s", "c", "shell", {"command": "pwd", "workdir": "/etc"})
    with pytest.raises(PermissionError):
        plugin_scope(runtime, "s", "c2", "read", {"path": "/etc/passwd"})
    for timeout in [-1, 0, "100", True]:
        with pytest.raises(ValueError):
            plugin_scope(runtime, "s", "bad", "shell", {"command": "pwd", "timeout": timeout})


@pytest.mark.asyncio
@pytest.mark.skipif(__import__("sys").platform != "linux", reason="requires Linux shell and flock")
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
    auth = plugin_scope(runtime, "s", "native", "shell", {"command": command})
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
    authorization = plugin_scope(runtime, "s", "ro", "shell", {"command": "pwd", "timeout": 999999})
    argv = authorization["argv"]
    assert "--bind" not in argv and "--share-net" not in argv
    assert argv[argv.index(workspace) - 1] == "--ro-bind"
    assert 0 < authorization["timeout_ms"] <= 60000
    policy.filesystem.workspace = "none"
    argv = plugin_scope(runtime, "s", "none", "shell", {"command": "pwd"})["argv"]
    assert workspace not in argv
    assert argv[argv.index("--chdir") + 1] == "/"
    with pytest.raises(PermissionError):
        plugin_scope(runtime, "s", "denied-cwd", "shell", {"command": "pwd", "workdir": workspace})
    row = runtime.db.get("run", "r")
    row["submit"]["deadline_at"] = time.time() - 1
    runtime.db.put("run", "r", row)
    with pytest.raises(PermissionError, match="deadline"):
        plugin_scope(runtime, "s", "expired", "shell", {"command": "pwd"})


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
    assert runtime.opencode.aborted == ["s"]
    assert runtime.db.get("run", "r")["status"] == "cancelled"
    assert runtime.db.get("run", "bob-run")["status"] == "running"
    assert runtime.db.get("run", "other-run")["status"] == "queued"
    assert runtime.db.get("run", "frontend-run")["status"] == "queued"
    assert runtime.db.get("run", "cancel")["status"] == "completed"
    assert "cancel" not in runtime.tasks
    await runtime.on_command(event)
    assert runtime.opencode.aborted == ["s"]


@pytest.mark.asyncio
async def test_slash_cancel_idle_does_not_start_model(runtime):
    submit = active(runtime)
    runtime.db.put("run", "r", {**runtime.db.get("run", "r"), "status": "completed"})
    cancel = submit.model_copy(update={"request_id": "cancel", "content": "/cancel"})
    await runtime.on_command(Envelope(type="request.submit", bucket_id="b", request_id="cancel",
                                     payload=cancel.model_dump(mode="json")))
    assert runtime.db.get("run", "cancel")["status"] == "completed"
    assert runtime.opencode.aborted == []
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
async def test_opencode_process_passes_watched_file_and_trusted_plugin(runtime, monkeypatch):
    import json
    oc = runtime.config.opencode
    oc.binary.write_text("binary")
    oc.binary.chmod(0o555)
    oc.config_file.write_text(json.dumps({"model": "test/first", "plugins": ["custom-plugin"]}))
    (oc.plugin_dir / "dist").mkdir(parents=True)
    (oc.plugin_dir / "package.json").write_text("{}")
    (oc.plugin_dir / "dist/index.mjs").write_text("plugin")
    captured = {}
    async def spawn(*args, **kwargs):
        captured.update(kwargs)
        class Process:
            returncode = None
            def terminate(self): self.returncode = 0
            async def wait(self): return self.returncode
        return Process()
    async def healthy(): return True
    opencode = OpenCodeServer(runtime.config)
    monkeypatch.setattr("bucket_runtime.opencode_server.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr(opencode, "health", healthy)
    try:
        await opencode._start_process()
        env = captured["env"]
        assert env["OPENCODE_CONFIG"] == str(oc.config_file.resolve())
        assert json.loads(env["OPENCODE_CONFIG_CONTENT"]) == {"plugins": [oc.plugin_dir.resolve().as_uri()]}
        assert env["OPENCODE_PASSWORD"] == opencode.password
        assert not (runtime.config.state_root / "opencode-home/config/opencode/node_modules").exists()
        assert json.loads(oc.config_file.read_text())["plugins"] == ["custom-plugin"]
    finally:
        await opencode.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_slash_cancel_keeps_user_lock_until_cleanup_finishes(runtime, cleanup_fails):
    started = asyncio.Event()
    stopping, settled, acquired = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def create_session(permissions, context, *, directory=None):
        assert context["user_id"] == "u"
        assert context["requests"][context["input_message_id"]] == "live"
        return "live-session"

    async def submit_message(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    async def abort(session_id, *, directory=None):
        runtime.opencode.aborted.append(session_id)
        native(runtime, session_id, "session.execution.interrupted", {"reason": "user"})
        stopping.set()
        await settled.wait()
        if cleanup_fails:
            raise TimeoutError("cleanup still running")

    runtime.opencode.create_session = create_session
    runtime.opencode.submit = submit_message
    runtime.opencode.abort = abort
    submit = Submit(request_id="live", chat_id="chat", identity=Identity(frontend_id="web", user_id="u"),
                    content="hello", deadline_at=time.time() + 60)
    await runtime.on_command(Envelope(type="request.submit", bucket_id="b", request_id="live",
                                     payload=submit.model_dump(mode="json")))
    await asyncio.wait_for(started.wait(), 2)
    cancel = submit.model_copy(update={"request_id": "stop", "content": "/cancel"})
    cancelling = asyncio.create_task(runtime.on_command(Envelope(type="request.submit", bucket_id="b",
        request_id="stop", payload=cancel.model_dump(mode="json"))))

    async def next_request():
        async with runtime.request_locks[submit.identity.identity_id]:
            acquired.set()

    following = asyncio.create_task(next_request())
    try:
        await asyncio.wait_for(stopping.wait(), 2)
        assert not acquired.is_set()
        assert not cancelling.done()
        assert runtime.db.get("run", "live")["status"] == "running"
        assert not runtime.completions["live"].is_set()
        settled.set()
        await asyncio.wait_for(cancelling, 2)
        await asyncio.wait_for(following, 2)
    finally:
        settled.set()
        await asyncio.gather(cancelling, following, *runtime.tasks.values(), return_exceptions=True)
    assert runtime.opencode.aborted == ["live-session"]
    if cleanup_fails:
        assert runtime.draining
        assert runtime.db.get("run", "live")["result"]["error"] == "CANCEL_FAILED"
        assert runtime.db.get("run", "stop")["result"]["error"] == "CANCEL_FAILED"
    else:
        assert runtime.db.get("run", "live")["status"] == "cancelled"
        assert runtime.db.get("run", "stop")["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_cancel_without_task_waits_despite_early_native_terminal(runtime, cleanup_fails):
    active(runtime)
    runtime.completions["r"] = asyncio.Event()
    waiting, settled = asyncio.Event(), asyncio.Event()
    client = OpenCodeServer(runtime.config)
    await client.http.aclose()

    async def handle(request):
        if request.url.path == "/api/session/s/interrupt":
            native(runtime, "s", "session.execution.interrupted", {"reason": "user"})
            return httpx.Response(200, json={"interrupted": True})
        assert request.url.path == "/api/experimental/session/s/wait"
        waiting.set()
        await settled.wait()
        return httpx.Response(503 if cleanup_fails else 204)

    async with httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(handle)) as client.http:
        runtime.opencode.abort = client.abort
        cancelling = asyncio.create_task(runtime.cancel_request("r"))
        try:
            await asyncio.wait_for(waiting.wait(), 2)
            assert not cancelling.done()
            assert runtime.db.get("run", "r")["status"] == "running"
            assert not runtime.completions["r"].is_set()
            assert not any(e.type == "runtime.result" for e in runtime.db.pending("gateway"))
            settled.set()
            if cleanup_fails:
                with pytest.raises(RuntimeError, match="CANCEL_FAILED"):
                    await asyncio.wait_for(cancelling, 2)
                assert runtime.draining
                assert runtime.db.get("run", "r")["result"]["error"] == "CANCEL_FAILED"
            else:
                await asyncio.wait_for(cancelling, 2)
                assert runtime.db.get("run", "r")["status"] == "cancelled"
        finally:
            cancelling.cancel()
            await asyncio.gather(cancelling, return_exceptions=True)



def test_user_scope_compiles_session_paths_and_checks_native_tools(runtime, tmp_path):
    import fnmatch

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
        pattern = path.relative_to(workspace).as_posix() if path.is_relative_to(workspace) else path.as_posix()
        matching = [r for r in rules if fnmatch.fnmatchcase(name, r["action"])
                    and fnmatch.fnmatchcase(pattern, r["resource"])]
        return matching[-1]["effect"] == "allow"

    assert allowed("read", workspace / "file.txt")
    assert allowed("edit", workspace / "file.txt")
    assert allowed("read", public / "SKILL.md")
    assert not allowed("edit", public / "SKILL.md")
    assert not allowed("read", bob_workspace / "file.txt")
    assert not allowed("read", tmp_path / "secret")
    normalized = plugin_scope(runtime, "s", "read", "read", {"path": "file.txt"})
    assert normalized["args"]["path"] == str(workspace / "file.txt")
    for tool, args in [("read", {"path": str(bob_workspace / "file.txt")}),
                       ("write", {"path": str(public / "SKILL.md")}),
                       ("glob", {"path": str(tmp_path), "pattern": "*"}),
                       ("webfetch", {"url": "https://example.com"})]:
        with pytest.raises(PermissionError):
            plugin_scope(runtime, "s", tool, tool, args)
    with pytest.raises(PermissionError):
        plugin_scope(runtime, "bob", "edit", "edit", {"path": str(bob_workspace / "file.txt")})
    with pytest.raises(PermissionError):
        plugin_scope(runtime, "bob", "read", "read", {"path": str(public / "SKILL.md")})


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
        plugin_scope(runtime, "s", "patch", "patch", {"patchText":
            "*** Begin Patch\n*** Update File: a\n*** Move to: ../escape\n*** End Patch\n"})
    link = own / "link"
    try:
        link.symlink_to(tmp_path / "private")
    except OSError:
        pytest.skip("symlink privileges unavailable")
    with pytest.raises(PermissionError):
        plugin_scope(runtime, "s", "read", "read", {"path": str(link)})


def test_individual_write_switch_enables_shared_edit_permission(runtime):
    from agent_backend.config import PermissionConfig
    runtime.permissions = PermissionConfig.model_validate({"defaults": {
        "session": {"default_tools": False, "tools": {"write": True}}}})
    submit = active(runtime)
    rules = runtime.session_permissions(submit)
    assert any(rule["action"] == "edit" and rule["effect"] == "allow" for rule in rules)
    assert plugin_scope(runtime, "s", "write", "write", {"path": "new.txt"})["args"]["path"]
    with pytest.raises(PermissionError):
        plugin_scope(runtime, "s", "edit", "edit", {"path": "new.txt"})


@pytest.mark.asyncio
async def test_publish_tool_uploads_through_gateway_without_control_peer(runtime, tmp_path, monkeypatch):
    import hashlib

    from agent_backend.models import Attachment
    submit = active(runtime)
    own = runtime.workspace(submit)
    source = own / "report.txt"
    source.write_bytes(b"report bytes")
    uploaded = []
    async def gateway(request):
        data = await request.aread()
        uploaded.append(data)
        assert request.url.path.startswith("/internal/attachments/web/r/att_")
        assert request.headers["x-bucket-id"] == "b"
        assert request.url.params["sha256"] == hashlib.sha256(data).hexdigest()
        return httpx.Response(200, json=Attachment(attachment_id=request.url.path.rsplit("/", 1)[1],
            filename=request.url.params["filename"], size=len(data), sha256=request.url.params["sha256"],
            storage="minio", url="https://objects.example/reports/report.txt").model_dump())
    monkeypatch.setattr(runtime.artifacts, "client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(gateway), base_url="http://gateway"))
    body = publication(runtime, "s", "publish", [{"path": "report.txt"}])
    result = await runtime.publish(body)
    assert runtime.peer is None
    assert uploaded == [b"report bytes"]
    attachment = result["attachments"][0]
    assert attachment["url"].startswith("https://objects.example/reports/")
    assert runtime.db.get("run", "r")["artifacts"] == result["attachments"]
    assert await runtime.publish(body) == result
    assert len(uploaded) == 1


@pytest.mark.asyncio
async def test_failed_gateway_upload_does_not_register_an_attachment(runtime, tmp_path, monkeypatch):
    submit = active(runtime)
    (runtime.workspace(submit) / "report.txt").write_text("report")
    monkeypatch.setattr(runtime.artifacts, "client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(502)), base_url="http://gateway"))
    body = publication(runtime, "s", "publish", [{"path": "report.txt"}])
    with pytest.raises(httpx.HTTPStatusError):
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

    async def session(session_id, *, directory=None):
        queried.append(session_id)
        return {"location": {"directory": str(directory)}, "parentID": "s" if session_id == "child" else None}

    runtime.opencode.session = session
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
