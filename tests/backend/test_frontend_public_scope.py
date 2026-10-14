import json
import shutil
import sys
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent_backend.common import frontend_segment
from agent_backend.config import RuntimeConfig, UserPolicy
from agent_backend.models import Envelope, Identity, Submit
from bucket_runtime.permissions import public_roots
from bucket_runtime.runtime import Runtime
from bucket_runtime.sandbox_executor import SandboxExecutor
from bucket_runtime.supervisor import Supervisor


@pytest.fixture
def tenants(tmp_path):
    common = tmp_path / "common"
    (common / "frontend.json").parent.mkdir()
    (common / "frontend.json").write_text("service-secret")
    runtimes = []
    for frontend in ["web-a", "web-b"]:
        own_public = common / frontend / "app"
        own_public.mkdir(parents=True)
        (common / frontend / "config").mkdir()
        (common / frontend / "config/future-secret.txt").write_text("private")
        (own_public / "manual.txt").write_text(frontend)
        (own_public / "skills").mkdir()
        cfg = RuntimeConfig(bucket_id=frontend, frontend_id=frontend, common_root=common,
            state_root=tmp_path / frontend / "state", workspace_root=tmp_path / "users",
            opencode={"context_root": tmp_path / "context"},
            frontend_config_file=common / "frontend.json")
        runtime = Runtime(cfg, SimpleNamespace(), SandboxExecutor(cfg.sandbox, 4096))
        submit = Submit(request_id=frontend, chat_id=frontend,
            identity=Identity(frontend_id=frontend, user_id="alice"), content="test",
            deadline_at=time.time()+60)
        runtime.db.put("run", frontend, {"request_id": frontend, "status": "running",
            "session_id": frontend, "submit": submit.model_dump(mode="json"), "artifacts": []})
        runtimes.append((runtime, submit))
    yield common, runtimes
    for runtime, _ in runtimes:
        runtime.db.close()


@pytest.mark.parametrize("explicit_empty", [False, True])
def test_native_read_search_write_and_session_rules_are_frontend_scoped(tenants, explicit_empty):
    import fnmatch
    import os
    common, pairs = tenants
    for runtime, submit in pairs:
        if explicit_empty:
            runtime.permissions.defaults.filesystem.public_roots = []
        frontend = submit.identity.frontend_id
        other = "web-b" if frontend == "web-a" else "web-a"
        assert runtime.public_roots(submit) == [common / frontend / "app"]
        assert runtime.authorize(frontend, "r", "read", {
            "filePath": str(common / frontend / "app/manual.txt")})["args"]["filePath"] == str(common / frontend / "app/manual.txt")
        for tool, args in [
            ("read", {"filePath": str(common / other / "app/manual.txt")}),
            ("read", {"filePath": str(common / "frontend.json")}),
            ("read", {"filePath": str(common / frontend / "app/../../" / other / "app/manual.txt")}),
            ("read", {"filePath": str(common / frontend / "config/future-secret.txt")}),
            ("read", {"filePath": str(common / frontend / "app/../config/future-secret.txt")}),
            ("grep", {"path": str(common), "pattern": "secret"}),
            ("grep", {"path": str(common / frontend), "pattern": "secret"}),
            ("grep", {"path": str(common / frontend / "config"), "pattern": "secret"}),
            ("glob", {"path": str(common / other), "pattern": "*"}),
            ("glob", {"path": str(common / frontend / "app"), "pattern": "../config/*"}),
            ("list", {"path": str(common)}),
            ("list", {"path": str(common / frontend / "config")}),
            ("lsp", {"filePath": str(common / other / "manual.txt")}),
            ("write", {"filePath": str(common / frontend / "app/manual.txt"), "content": "overwrite"}),
        ]:
            with pytest.raises(PermissionError):
                runtime.authorize(frontend, "denied", tool, args)
        rules = runtime.session_permissions(submit)
        for path, expected in [(common / frontend / "app/manual.txt", "allow"),
                               (common / frontend / "config/future-secret.txt", "deny"),
                               (common / frontend, "deny"),
                               (common / other / "app/manual.txt", "deny"), (common / "frontend.json", "deny")]:
            pattern = os.path.relpath(path, runtime.worktree(submit)).replace("\\", "/")
            applicable = [r for r in rules if fnmatch.fnmatchcase("read", r["permission"])
                          and fnmatch.fnmatchcase(pattern, r["pattern"])]
            assert applicable[-1]["action"] == expected


def test_user_override_cannot_grant_other_frontend_or_parent(tenants):
    common, pairs = tenants
    runtime, _ = pairs[0]
    for path in [common, common.parent, common / "web-a", common / "web-a/config",
                 common / "web-a/config/future-secret.txt", common / "web-b/app", common / "web-b/app/manual.txt"]:
        policy = UserPolicy.model_validate({"filesystem": {"public_roots": [path]}})
        with pytest.raises(ValueError, match="current frontend"):
            public_roots(runtime.config, policy)
    assert public_roots(runtime.config, UserPolicy.model_validate({"filesystem": {
        "public_roots": [common / "web-a/app/skills"]}})) == [common / "web-a/app/skills", common / "web-a/app"]
    assert public_roots(runtime.config, UserPolicy.model_validate({"filesystem": {"public_roots": []}})) == [common / "web-a/app"]


@pytest.mark.parametrize("suffix", ["", "web-a", "web-a/config", "web-b/app"])
def test_broad_sandbox_mount_is_rejected(tenants, suffix):
    common, pairs = tenants
    raw = pairs[0][0].config.model_dump()
    raw["sandbox"]["readonly_roots"].append(common / suffix)
    with pytest.raises(ValidationError, match="current frontend"):
        RuntimeConfig.model_validate(raw)


@pytest.mark.parametrize("destination", ["web-b/app", "web-a/config"])
def test_symlink_cannot_redirect_frontend_public_directory(tenants, destination):
    common, pairs = tenants
    runtime, _ = pairs[0]
    link = common / "web-a/app/link"
    try:
        link.symlink_to(common / destination, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    policy = UserPolicy.model_validate({"filesystem": {"public_roots": [link]}})
    with pytest.raises(ValueError):
        public_roots(runtime.config, policy)
    with pytest.raises(PermissionError):
        runtime.authorize("web-a", "link", "read", {"filePath": str(link / "future-secret.txt")})

    outside_link = common.parent / "alias"
    outside_link.symlink_to(common / destination, target_is_directory=True)
    policy.filesystem.public_roots = [outside_link]
    with pytest.raises(ValueError):
        public_roots(runtime.config, policy)


@pytest.mark.asyncio
async def test_foreign_frontend_is_rejected_before_workspace_or_journal_mutation(tenants):
    _, pairs = tenants
    runtime, _ = pairs[0]
    foreign = pairs[1][1].model_copy(update={"request_id": "foreign"})
    with pytest.raises(PermissionError, match="another frontend"):
        await runtime.on_command(Envelope(type="request.submit", bucket_id="web-a",
            request_id="foreign", payload=foreign.model_dump(mode="json")))
    assert runtime.db.get("run", "foreign") is None
    assert not runtime.config.workspace_root.exists()


@pytest.mark.asyncio
async def test_skill_discovery_is_scoped_and_broad_configuration_rejected(tenants, tmp_path):
    common, pairs = tenants
    for runtime, submit in pairs:
        path = tmp_path / (submit.identity.frontend_id + ".json")
        runtime.config.opencode.config_file = path
        supervisor = Supervisor(runtime.config)
        try:
            instructions = [str(common / submit.identity.frontend_id / "app/templates/AGENTS.md")]
            path.write_text(json.dumps({"instructions": instructions,
                "skills": {"paths": [str(common / "{frontend_id}/app/skills")]}}))
            assert supervisor.server_config()["instructions"] == instructions
            assert supervisor.server_config()["skills"]["paths"] == [str(common / submit.identity.frontend_id / "app/skills")]
            for wrong in [common, common / submit.identity.frontend_id, common / submit.identity.frontend_id / "config",
                          common / ("web-b" if submit.identity.frontend_id == "web-a" else "web-a") / "app"]:
                path.write_text(json.dumps({"skills": {"paths": [str(wrong)]}}))
                with pytest.raises(ValueError, match="current frontend"):
                    supervisor.server_config()
        finally:
            await supervisor.stop()


@pytest.mark.parametrize("frontend", ["../other", "/other", "a/b", "a\\b", "..", "*", "frontend.json"])
def test_frontend_names_cannot_escape_or_collide_with_service_config(frontend):
    with pytest.raises(ValueError):
        frontend_segment(frontend)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"), reason="requires Linux bubblewrap")
async def test_bash_can_only_read_own_frontend_and_cannot_modify_public_files(tenants):
    common, pairs = tenants
    for runtime, submit in pairs:
        own = common / submit.identity.frontend_id / "app"
        other = common / ("web-b" if submit.identity.frontend_id == "web-a" else "web-a")
        workspace = runtime.workspace(submit)
        result = await runtime.executor.execute(submit.request_id, workspace, runtime.public_roots(submit),
            f"test ! -e '{other}' && test ! -e '{own.parent / 'config'}' && "
            f"test ! -e '{common / 'frontend.json'}' && cat '{own / 'manual.txt'}'")
        assert result == {"output": submit.identity.frontend_id, "exit_code": 0}
        result = await runtime.executor.execute(submit.request_id, workspace, runtime.public_roots(submit),
            f"printf changed > '{own / 'manual.txt'}'")
        assert result["exit_code"] != 0
        assert (own / "manual.txt").read_text() == submit.identity.frontend_id
