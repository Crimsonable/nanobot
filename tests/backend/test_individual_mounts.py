import json
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_backend.config import FrontendConfig, individual_bucket_id
from agent_backend.journal import Journal
from agent_backend.models import Identity
from container_up.bucket_manager import BucketManager
from container_up.bucket_scheduler import BucketScheduler


@pytest.fixture
def setup(tmp_path):
    common = tmp_path / "common"
    config_dir = common / "web/config"
    config_dir.mkdir(parents=True)
    policy = config_dir / "user-permissions.yaml"
    policy.write_text("defaults: {filesystem: {workspace: read_only}}\n")
    frontend = FrontendConfig(frontend_id="web", provider="web", bucket={
        "behavior": "individual", "common_root": common,
        "mounts": [{"name": "docs", "volume": "common", "sub_path": "shared/docs",
                    "mount_path": "/mnt/shared/docs", "access": "read_only"}]})
    control = frontend.control_config()
    db = Journal(tmp_path / "control.sqlite")
    owner = Identity(frontend_id="web", user_id="alice")
    bid = individual_bucket_id(owner)
    bucket = BucketScheduler.new_bucket(bid, owner, frontend.bucket.behavior)
    db.put("bucket", bid, bucket)
    manager = BucketManager(control, db)
    yield manager, db, owner, bid, policy
    db.close()


def test_individual_mounts_only_user_and_bucket_data_with_native_shell_config(setup):
    manager, db, owner, bid, _ = setup
    deployment = manager.deployment(bid, "web", "test")
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    mounts = {m["name"]: m for m in container["volumeMounts"]}
    assert mounts["workspace"] == {"name": "workspace", "mountPath": "/mnt/env/workspaces/web/alice",
                                   "subPath": "workspaces/web/alice", "readOnly": True}
    assert mounts["data"]["mountPath"] == f"/data/runtime/web/{bid}"
    assert mounts["data"]["subPath"] == f"runtime/web/{bid}"
    assert mounts["source"]["mountPath"] == "/mnt/source/opencode_plugin"
    assert mounts["source"]["readOnly"]
    assert mounts["shared-docs"]["subPath"] == "shared/docs"
    assert mounts["shared-docs"]["readOnly"]
    all_mounts = container["volumeMounts"]
    assert not any(m["mountPath"] in ("/data", "/mnt/env", "/mnt/env/workspaces/web") for m in all_mounts)
    assert not any(m.get("subPath") == "frontends.json" for m in all_mounts)
    assert next(m for m in all_mounts if m.get("subPath") == "web/config")["readOnly"]
    assert container["securityContext"]["privileged"] is False
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    env = {e["name"]: e["value"] for e in container["env"]}
    runtime = json.loads(env["MANAGED_BUCKET"])
    assert runtime["behavior"] == "individual"
    assert runtime["owner"] == owner.model_dump()
    assert "individual_policy" not in runtime
    assert runtime["permissions_file"].endswith("user-permissions.yaml")
    assert "settings" not in db.get("bucket", bid) and "runtime" not in db.get("bucket", bid)
    assert runtime["frontend_config_file"] is None


def test_mount_permissions_are_resolved_for_owner_and_allow_explicit_shared_writes(setup):
    manager, _, _, bid, policy = setup
    policy.write_text("""defaults:
  filesystem:
    workspace: read_only
users:
  - user_id: alice
    filesystem:
      workspace: read_write
      mounts: {docs: read_write}
  - user_id: bob
    filesystem:
      workspace: none
""")
    mounts = {m["name"]: m for m in manager.deployment(bid, "web", "test")["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]}
    assert mounts["workspace"]["readOnly"] is False
    assert mounts["shared-docs"]["readOnly"] is False
    policy.write_text("defaults: {filesystem: {workspace: none, mounts: {docs: none}}}")
    mounts = {m["name"]: m for m in manager.deployment(bid, "web", "test")["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]}
    assert "workspace" not in mounts and "shared-docs" not in mounts
    assert mounts["empty-workspace"]["readOnly"]


def test_bucket_deployment_keeps_its_mode_but_reads_current_frontend_configuration(setup):
    manager, db, owner, bid, _ = setup
    manager.config.bucket_templates["web"].behavior = "pool"
    manager.config.bucket_templates["web"].opencode.port = 8888
    runtime = manager.runtime(bid, "web")
    assert runtime.behavior == "individual" and runtime.owner == owner
    assert runtime.opencode.port == 8888
    old = "opencode-bucket-web-0123456789ab"
    db.put("bucket", old, {"bucket_id": old, "frontend_id": "web", "behavior": "pool", "owner": None})
    manager.config.bucket_templates["web"].behavior = "individual"
    assert manager.runtime(old, "web").behavior == "pool"


@pytest.mark.asyncio
async def test_native_endpoint_checks_owner_and_uses_bucket_port(setup):
    manager, _, _, bid, policy = setup
    body = manager.deployment(bid, "web", "test")
    policy.unlink()  # A permission edit must not prevent collecting output from an existing Pod.
    metadata = {**body["metadata"], "uid": "generation"}
    manager.request = AsyncMock(return_value=httpx.Response(200, request=httpx.Request("GET", "http://kubernetes/pods"), json={"items": [
        {"metadata": metadata, "spec": body["spec"]["template"]["spec"], "status": {"phase": "Running", "podIP": "10.0.0.2"}}]}))
    manager.config.bucket_templates["web"].opencode.port = 8888
    assert (await manager.endpoint(bid, "web", "test"))["url"] == "http://10.0.0.2:4096"
    metadata["annotations"]["opencode.io/owner"] = "foreign-owner"
    manager.request = AsyncMock(return_value=httpx.Response(200, request=httpx.Request("GET", "http://kubernetes/pods"), json={"items": [
        {"metadata": metadata, "spec": body["spec"]["template"]["spec"], "status": {"phase": "Running", "podIP": "10.0.0.2"}}]}))
    with pytest.raises(RuntimeError, match="ownership"):
        await manager.endpoint(bid, "web", "test")


@pytest.mark.parametrize("patch", [
    {"sandbox": {"network": False}},
    {"filesystem": {"workspace": "invalid"}},
    {"filesystem": {"mounts": {"unknown": "read_write"}}},
    {"filesystem": {"public_roots": ["/other"]}},
    {"session": {"tools": {"shell": "yes"}}},
])
def test_incompatible_policy_is_not_silently_ignored(setup, patch):
    manager, _, _, bid, policy = setup
    policy.write_text(json.dumps({"defaults": patch}))
    with pytest.raises(ValueError):
        manager.deployment(bid, "web", "test")


def test_individual_can_use_default_mount_permissions_without_policy_file(setup):
    manager, _, _, bid, _ = setup
    manager.config.bucket_templates["web"].permissions_file = None
    pod = manager.deployment(bid, "web", "test")["spec"]["template"]["spec"]
    assert next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "workspace")["readOnly"] is False


def test_mounts_cannot_expose_other_frontend_or_override_service_paths(setup):
    manager, _, _, bid, _ = setup
    mount = manager.config.bucket_templates["web"].mounts[0]
    mount.sub_path = "other/config"
    with pytest.raises(ValueError, match="shared/"):
        manager.runtime(bid, "web")
    mount.sub_path, mount.mount_path = "shared/docs", "/data"
    with pytest.raises(ValueError, match="below /mnt/shared"):
        manager.runtime(bid, "web")


def test_directory_preparation_precedes_individual_deployment(setup, tmp_path):
    manager, _, _, bid, _ = setup
    runtime = manager.runtime(bid, "web")
    # This helper uses actual paths; the Deployment generator retains the PVC paths.
    runtime.state_root = tmp_path / "state"
    runtime.workspace_root = tmp_path / "users"
    manager.prepare_storage(runtime)
    assert runtime.state_root.is_dir()
    assert (runtime.workspace_root / "web/alice").is_dir()


def test_manifest_upgrade_keeps_individual_owner_after_mode_switch(setup):
    from agent_backend.config import FrontendsConfig
    from scripts.render_bucket_updates import render

    manager, _, _, bid, policy = setup
    current = manager.deployment(bid, "web", "test")
    current["spec"]["replicas"] = 0
    settings = manager.config.bucket_templates["web"].model_dump()
    settings.update(behavior="pool", opencode={"port": 8888})
    config = FrontendsConfig(frontends=[{"frontend_id": "web", "provider": "web", "bucket": settings}])
    policy.write_text('{"defaults": {"filesystem": {"workspace": "read_write"}}}')
    result = render(config, {"items": [current]})["items"][0]
    assert result["spec"]["replicas"] == 0
    assert result["metadata"]["annotations"]["opencode.io/behavior"] == "individual"
    mounts = result["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert next(m for m in mounts if m["name"] == "workspace")["subPath"] == "workspaces/web/alice"
    assert next(m for m in mounts if m["name"] == "workspace")["readOnly"] is False
    assert result["spec"]["template"]["spec"]["containers"][0]["ports"][0]["containerPort"] == 8888


def test_json_permissions_are_reread_for_each_deployment(setup):
    manager, db, _, bid, yaml_file = setup
    policy = yaml_file.with_suffix(".json")
    manager.config.bucket_templates["web"].permissions_file = policy
    policy.write_text(json.dumps({"defaults": {"filesystem": {"workspace": "read_only"}}}))
    first = manager.deployment(bid, "web", "test")
    policy.write_text(json.dumps({"defaults": {"filesystem": {"workspace": "read_write"}}}))
    second = manager.deployment(bid, "web", "test")
    for body, readonly in [(first, True), (second, False)]:
        container = body["spec"]["template"]["spec"]["containers"][0]
        assert next(m for m in container["volumeMounts"] if m["name"] == "workspace")["readOnly"] is readonly
        env = {e["name"]: e["value"] for e in container["env"]}
        assert "individual_policy" not in json.loads(env["MANAGED_BUCKET"])
    assert "settings" not in db.get("bucket", bid) and "runtime" not in db.get("bucket", bid)


@pytest.mark.asyncio
async def test_ensure_does_not_persist_generated_configuration(setup, monkeypatch):
    manager, db, _, bid, _ = setup
    original = db.get("bucket", bid)
    body = manager.deployment(bid, "web", "test")
    body["metadata"]["resourceVersion"] = "1"
    manager.request = AsyncMock(side_effect=[
        httpx.Response(200, json=body, request=httpx.Request("GET", "http://kubernetes/deployment")),
        httpx.Response(200, json={}, request=httpx.Request("PUT", "http://kubernetes/deployment")),
    ])
    manager.scale = AsyncMock()
    monkeypatch.setattr(manager, "prepare_storage", lambda runtime: None)
    await manager.ensure(bid, "web", "test")
    assert db.get("bucket", bid) == original
    manager.scale.assert_awaited_once_with(bid, 1)
