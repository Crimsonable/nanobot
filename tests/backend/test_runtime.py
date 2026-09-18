import asyncio
import time

import pytest

from agent_backend.config import RuntimeConfig
from agent_backend.models import Envelope, Identity, Submit
from bucket_runtime.runtime import Runtime


class Client:
    def __init__(self):
        self.aborted = []

    async def abort(self, session_id):
        self.aborted.append(session_id)


class Executor:
    def __init__(self):
        self.executions = []

    async def execute(self, request_id, workspace, public, command, workdir, timeout):
        self.executions.append((request_id, command))
        return {"output": "ok", "exit_code": 0}

    async def kill(self, request_id):
        pass


@pytest.fixture
def runtime(tmp_path):
    config = RuntimeConfig.model_validate({
        "bucket_id": "b", "control_url": "ws://localhost:8000/internal/buckets/ws",
        "credential_file": str(tmp_path / "credential"), "state_root": str(tmp_path / "state"),
        "workspace_root": str(tmp_path / "workspaces"),
        "opencode": {"binary": str(tmp_path / "binary"), "sha256": "a" * 64,
                     "context_root": str(tmp_path / "context"), "config_file": str(tmp_path / "model.json"),
                     "plugin_file": str(tmp_path / "plugin"), "dependency_dir": str(tmp_path / "deps")},
        "profiles": {"p": {"model": "test/test"}},
    })
    instance = Runtime(config, Client(), Executor())
    yield instance
    instance.db.close()


def active(runtime, name="alice", request_id="r", session_id="s"):
    submit = Submit(request_id=request_id, conversation_id="conversation-" + name,
        identity=Identity(frontend_id="web", user_id=name), profile_id="p", content="hello",
        deadline_at=time.time() + 60)
    runtime.db.put("run", request_id, {"request_id": request_id, "status": "running",
        "session_id": session_id, "submit": submit.model_dump(mode="json"), "artifacts": []})
    return submit


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
