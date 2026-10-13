import json
import shutil
import sys
import time
from types import SimpleNamespace

import pytest

from agent_backend.config import FrontendsConfig, load_frontends
from agent_backend.models import Identity, Submit
from bucket_runtime.runtime import Runtime
from bucket_runtime.sandbox_executor import SandboxExecutor
from bucket_runtime.supervisor import Supervisor
from container_up.bucket_manager import BucketManager
from container_up.server import ControlService, Inbound


@pytest.fixture
def registry(tmp_path):
    common = tmp_path / "common"
    entries = []
    for frontend in ["web-a", "web-b"]:
        own = common / frontend
        (own / "app/skills").mkdir(parents=True)
        (own / "app/templates").mkdir()
        (own / "app/templates/README.txt").write_text(frontend)
        (own / "app/manual.txt").write_text(frontend)
        (own / "config").mkdir()
        (own / "config/future-secret.txt").write_text("not-a-known-config-file")
        (own / "config/opencode-server.json").write_text(json.dumps({
            "provider": {"local": {"options": {"apiKey": "model-secret-" + frontend}}},
            "skills": {"paths": [str(common / "{frontend_id}/app/skills")]}}))
        (own / "config/user-permissions.yaml").write_text(json.dumps({
            "defaults": {"filesystem": {"workspace": "read_only" if frontend == "web-a" else "read_write"}},
            "users": [{"user_id": "alice", "session": {"tools": {"bash": frontend == "web-b"}}}]}))
        entries.append({"id": frontend, "provider": "web", "instance_idle_ttl_seconds": 1 if frontend == "web-a" else 0,
            "runtime": {"common_root": str(common), "profiles": {"reports": {
                "model": "local/" + frontend, "public_roots": [str(common / "{frontend_id}/app")]}}}})
    path = common / "frontends.json"
    path.write_text(json.dumps({"frontends": entries}))
    return path, load_frontends(path)


def runtime_for(path, registry, frontend, tmp_path):
    config = registry.runtime_config(path, "opencode-bucket-0123456789ab", frontend)
    config.state_root = tmp_path / "state" / frontend
    config.workspace_root = tmp_path / "workspaces"
    config.opencode.context_root = tmp_path / "context"
    runtime = Runtime(config, SimpleNamespace(), SandboxExecutor(config.sandbox, 4096))
    submit = Submit(request_id=frontend, conversation_id=frontend,
        identity=Identity(frontend_id=frontend, user_id="alice"), profile_id="reports", content="hello",
        deadline_at=time.time() + 60)
    runtime.db.put("run", frontend, {"request_id": frontend, "status": "running", "session_id": frontend,
        "submit": submit.model_dump(mode="json"), "artifacts": []})
    return runtime, submit


@pytest.mark.asyncio
async def test_models_permissions_and_templates_are_selected_per_frontend(registry, tmp_path):
    path, config = registry
    for frontend in ["web-a", "web-b"]:
        runtime, submit = runtime_for(path, config, frontend, tmp_path)
        supervisor = Supervisor(runtime.config)
        try:
            assert runtime.config.profiles["reports"].model == "local/" + frontend
            native = supervisor.server_config()
            assert native["provider"]["local"]["options"]["apiKey"] == "model-secret-" + frontend
            assert native["skills"]["paths"] == [str(path.parent / frontend / "app/skills")]
            assert runtime.policy(submit).filesystem.workspace == ("read_only" if frontend == "web-a" else "read_write")
            assert runtime.policy(submit).session.tools["bash"] is (frontend == "web-b")
            assert (runtime.workspace(submit) / "README.txt").read_text() == frontend
            for filename in ["opencode-server.json", "user-permissions.yaml", "future-secret.txt"]:
                with pytest.raises(PermissionError, match="outside user permission scope"):
                    runtime.authorize(frontend, filename, "read", {"filePath": str(path.parent / frontend / "config" / filename)})
            runtime.authorize(frontend, "public", "read", {"filePath": str(path.parent / frontend / "app/manual.txt")})
            runtime.authorize(frontend, "search", "grep", {"path": str(path.parent / frontend / "app"), "pattern": "secret"})
        finally:
            runtime.db.close()
            await supervisor.stop()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"), reason="requires Linux bubblewrap")
async def test_entire_frontend_config_directory_is_absent_in_bash(registry, tmp_path):
    path, config = registry
    runtime, submit = runtime_for(path, config, "web-b", tmp_path)
    try:
        own = path.parent / "web-b"
        command = (f"test ! -e '{own / 'config'}' && test ! -e '{path}' && "
                   f"test ! -e '{path.parent / 'web-a'}' && cat '{own / 'app/manual.txt'}'")
        result = await runtime.executor.execute(submit.request_id, runtime.workspace(submit),
            runtime.public_roots(submit), command, private_files=runtime.private_files())
        assert result == {"output": "web-b", "exit_code": 0}
        assert "model-secret-web-b" in (own / "config/opencode-server.json").read_text()
    finally:
        runtime.db.close()


def test_cross_frontend_configuration_paths_fail_closed(registry):
    path, config = registry
    for section in ["opencode", "permissions_file", "workspace_template_root"]:
        frontend = config.frontend("web-a")
        wrong = path.parent / "web-b"
        if section == "opencode":
            frontend.runtime.opencode.config_file = wrong / "opencode-server.json"
        else:
            setattr(frontend.runtime, section, wrong / "user-permissions.yaml")
        with pytest.raises(ValueError, match="must belong to this frontend"):
            frontend.runtime_config(path, "opencode-bucket-0123456789ab")


@pytest.mark.parametrize("section", ["opencode", "permissions_file", "workspace_template_root", "storage"])
def test_configuration_and_content_cannot_swap_subtrees(registry, section):
    from agent_backend.config import StorageConfig

    path, config = registry
    frontend = config.frontend("web-a")
    own = path.parent / frontend.id
    if section == "opencode":
        frontend.runtime.opencode.config_file = own / "app/opencode-server.json"
    elif section == "permissions_file":
        frontend.runtime.permissions_file = own / "app/user-permissions.yaml"
    elif section == "storage":
        frontend.attachment_storage = StorageConfig(endpoint="http://minio:9000", bucket="test",
            access_key_file=own / "app/access-key", secret_key_file=own / "config/secret-key")
    else:
        frontend.runtime.workspace_template_root = own / "config"
    with pytest.raises(ValueError, match="must belong to this frontend"):
        frontend.runtime_config(path, "opencode-bucket-0123456789ab")


def test_config_symlink_cannot_alias_public_content(registry):
    path, config = registry
    frontend = config.frontend("web-a")
    own = path.parent / frontend.id
    link = own / "config/link"
    try:
        link.symlink_to(own / "app", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    frontend.runtime.permissions_file = link / "manual.txt"
    with pytest.raises(ValueError, match="without symbolic links"):
        frontend.runtime_config(path, "opencode-bucket-0123456789ab")


@pytest.mark.parametrize("section", ["sandbox", "session"])
@pytest.mark.parametrize("user_override", [False, True])
def test_frontend_config_boundary_cannot_be_disabled(registry, tmp_path, section, user_override):
    path, config = registry
    policy = {section: {"enabled": False}}
    raw = {"users": [{"user_id": "alice", **policy}]} if user_override else {"defaults": policy}
    (path.parent / "web-b/config/user-permissions.yaml").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="frontend isolation requires"):
        runtime_for(path, config, "web-b", tmp_path)


@pytest.mark.asyncio
async def test_one_cu_keeps_bucket_pools_and_retirement_frontend_scoped(registry, tmp_path):
    _, registry_config = registry
    config = registry_config.control_config()
    config.state_root = tmp_path / "control"
    for runtime in config.runtime_templates.values():
        runtime.workspace_root = tmp_path / "workspaces"
    service = ControlService(config)
    started, scaled = [], []

    async def ensure(bucket_id, frontend_id=None):
        started.append((bucket_id, frontend_id))
        service.peers[bucket_id] = SimpleNamespace(closed=False)

    async def scale(bucket_id, replicas):
        scaled.append((bucket_id, replicas))

    service.pool.manager = SimpleNamespace(ensure=ensure, scale=scale)
    try:
        first = await service.submit("web-a", Inbound(user_id="alice", content="hello"))
        second = await service.submit("web-b", Inbound(user_id="alice", content="hello"))
        assert first["bucket_id"] != second["bucket_id"]
        assert started == [(first["bucket_id"], "web-a"), (second["bucket_id"], "web-b")]
        assert {b["frontend_id"] for b in service.db.records("bucket")} == {"web-a", "web-b"}
        for request in service.db.records("request"):
            service.db.put("request", request["request_id"], {**request, "result": {"content": "done"}})
        for bucket in service.db.records("bucket"):
            service.db.put("bucket", bucket["bucket_id"], {**bucket, "last_active_at": time.time() - 10})
            for event in service.db.pending(bucket["bucket_id"]):
                service.db.ack(bucket["bucket_id"], event.seq, event.event_id)
        await service.pool.sweep()
        assert scaled == [(first["bucket_id"], 0)]
        other = await service.submit("web-b", Inbound(user_id="bob", content="hi"))
        assert other["bucket_id"] == second["bucket_id"]
        assert service.db.get("bucket", first["bucket_id"])["status"] == "idle"
    finally:
        service.db.close()


def test_bucket_manifest_and_upgrade_use_recorded_frontend(registry):
    from scripts.render_bucket_updates import render

    _, config = registry
    manager = BucketManager(config.control_config())
    workloads = [manager.deployment("opencode-bucket-0123456789ab", "web-a"),
                 manager.deployment("opencode-bucket-abcdef012345", "web-b")]
    for frontend, deployment in zip(["web-a", "web-b"], workloads):
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert container["args"][container["args"].index("--frontend-id") + 1] == frontend
        mounts = container["volumeMounts"]
        assert {m["subPath"] for m in mounts if m["name"] == "common"} == {frontend + "/app", frontend + "/config", "frontends.json"}
        assert {m["subPath"] for m in mounts if m["name"] == "workspace"} == {"workspaces/" + frontend}
    assert len(render(config, {"items": workloads})["items"]) == 2
    broken = config.control_config()
    del broken.runtime_templates["web-b"]
    with pytest.raises(ValueError, match="runtime is not configured"):
        broken.runtime_for("web-b")
    del workloads[0]["metadata"]["annotations"]
    with pytest.raises(ValueError, match="frontend_id"):
        render(config, {"items": workloads})


def test_registry_rejects_duplicate_ids_and_global_frontend_credentials(registry):
    path, config = registry
    raw = config.model_dump()
    assert FrontendsConfig.model_validate(raw).model_dump() == raw
    raw["frontends"][1]["id"] = raw["frontends"][0]["id"]
    with pytest.raises(ValueError, match="duplicate"):
        FrontendsConfig.model_validate(raw)
    raw["frontends"] = raw["frontends"][:1]
    raw["frontends"][0]["api_key_file"] = "/old/web-api-key"
    with pytest.raises(ValueError, match="Extra inputs"):
        FrontendsConfig.model_validate(raw)
    path.write_text(json.dumps({"id": "web-a", "provider": "web",
        "runtime": {"profiles": {"reports": {"model": "local/test"}}}}))
    with pytest.raises(ValueError, match="frontends"):
        load_frontends(path)
