from tests.backend.support import plugin_scope, publication
import shutil
import sys
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent_backend.config import FrontendConfig, RuntimeConfig
from agent_backend.models import Identity, Submit
from agent_backend.workspaces import workspace_path
from bucket_runtime.runtime import Runtime
from bucket_runtime.sandbox_executor import SandboxExecutor
from container_up.bucket_manager import BucketManager


@pytest.fixture
def workspaces(tmp_path):
    pairs = []
    for frontend in ["web-a", "web-b"]:
        config = RuntimeConfig(common_root=tmp_path / "common", bucket_id=frontend, frontend_id=frontend,
            state_root=tmp_path / frontend / "state", workspace_root=tmp_path / "workspaces",
             opencode={"context_root": tmp_path / "context"})
        runtime = Runtime(config, SimpleNamespace(), SandboxExecutor(config.sandbox, 4096))
        for user in ["alice", "bob"]:
            identity = Identity(frontend_id=frontend, user_id=user)
            submit = Submit(identity=identity, request_id=identity.identity_id, chat_id="default",
                content="hello", deadline_at=time.time() + 60)
            runtime.db.put("run", submit.request_id, {"request_id": submit.request_id,
                "status": "running", "session_id": identity.identity_id,
                "submit": submit.model_dump(mode="json"), "artifacts": []})
            workspace = runtime.workspace(submit)
            assert workspace.parent == tmp_path / "workspaces" / frontend
            (workspace / "report.txt").write_text(frontend + ":" + user)
            pairs.append((runtime, submit, workspace))
    yield pairs
    for runtime in {pair[0] for pair in pairs}:
        runtime.db.close()


def test_workspace_scope_is_frontend_and_user(workspaces):
    assert len({path for _, _, path in workspaces}) == 4
    for runtime, submit, workspace in workspaces:
        own_file = workspace / "report.txt"
        for tool in ["read", "write"]:
            result = plugin_scope(runtime, submit.identity.identity_id, tool, tool, {"filePath": str(own_file)})
            assert result["args"]["filePath"] == str(own_file)
        for _, other, other_workspace in workspaces:
            if other.identity == submit.identity:
                continue
            for tool, args in [
                ("read", {"filePath": str(other_workspace / "report.txt")}),
                ("write", {"filePath": str(other_workspace / "report.txt")}),
                ("grep", {"path": str(other_workspace), "pattern": "report"}),
                ("list", {"path": str(workspace.parent)}),
            ]:
                with pytest.raises(PermissionError):
                    plugin_scope(runtime, submit.identity.identity_id, "other", tool, args)
            if other.identity.frontend_id != submit.identity.frontend_id:
                with pytest.raises(PermissionError, match="another frontend"):
                    runtime.workspace(other)


@pytest.mark.asyncio
async def test_attachment_publication_cannot_use_other_frontend_workspace(workspaces):
    for runtime, submit, workspace in workspaces:
        uploads = []

        async def upload(identity, attachment_id, path, attachment):
            uploads.append((identity, path.read_text()))
            return attachment.model_copy(update={"url": "https://files.test/report.txt"})

        runtime.artifacts.upload = upload
        foreign = next(path for _, other, path in workspaces
                       if other.identity.frontend_id != submit.identity.frontend_id
                       and other.identity.user_id == submit.identity.user_id)
        body = publication(runtime, submit.identity.identity_id, "foreign", [{"path": str(foreign / "report.txt")}])
        with pytest.raises(PermissionError):
            await runtime.publish(body)
        assert uploads == []
        body = publication(runtime, submit.identity.identity_id, "own", [{"path": "report.txt"}])
        result = await runtime.publish(body)
        assert result["status"] == "queued_for_reply"
        assert uploads == [(submit.identity, (workspace / "report.txt").read_text())]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"), reason="requires Linux bubblewrap")
async def test_bash_mounts_only_the_current_frontend_user_workspace(workspaces):
    for runtime, submit, workspace in workspaces:
        checks = [f"test ! -e '{path}'" for _, _, path in workspaces if path != workspace]
        checks.extend(["printf mine > new.txt", "cat new.txt"])
        result = await runtime.executor.execute(submit.request_id, workspace, [], " && ".join(checks))
        assert result == {"output": "mine", "exit_code": 0}
        assert (workspace / "new.txt").read_text() == "mine"


def test_frontend_name_is_used_without_lossy_rewriting(tmp_path):
    for frontend in ["web", "-web", "业务前端"]:
        identity = Identity(frontend_id=frontend, user_id="alice")
        assert workspace_path(tmp_path, identity).parent == tmp_path / frontend
    with pytest.raises(ValueError):
        workspace_path(tmp_path, Identity(frontend_id="../web", user_id="alice"))


def test_custom_workspace_root_is_scoped_in_bucket_manifest():
    raw = {"frontend_id": "web-a", "provider": "web", "runtime": {
        "workspace_root": "/mnt/env/workspaces/custom"}}
    config = FrontendConfig.model_validate(raw)
    deployment = BucketManager(config.control_config()).deployment("opencode-bucket-0123456789ab", config.frontend_id)
    mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert [m for m in mounts if m["name"] == "workspace"] == [{"name": "workspace",
        "mountPath": "/mnt/env/workspaces/custom/web-a", "subPath": "workspaces/custom/web-a"}]
    for root in ["/mnt/env", "/mnt/env/workspaces/../../other", "relative/workspaces"]:
        raw["runtime"]["workspace_root"] = root
        with pytest.raises(ValidationError, match="workspace_root"):
            FrontendConfig.model_validate(raw)
