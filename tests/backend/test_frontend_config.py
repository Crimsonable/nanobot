import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_backend.config import FrontendConfig, StorageConfig, load_config
from bucket_runtime.artifact_service import ArtifactService

EXAMPLE = Path(__file__).resolve().parents[2] / "config/opencode/frontend.json"


def test_minimal_frontend_uses_deployment_defaults_without_bucket_credentials():
    config = FrontendConfig.model_validate({"id": "web-yk", "provider": "web",
        "send_msg_url": "http://frontend/outbound",
        "runtime": {"profiles": {"reports": {"model": "intranet/model"}}}})
    assert config.control.port == 8000
    assert config.control.bucket_pool.namespace == "opencode-sv"
    assert config.control.bucket_pool.data_pvc == "opencode-data-pvc"
    assert config.runtime.opencode.port == 4096
    assert config.runtime.workspace_root == Path("/mnt/env/workspaces")
    assert config.runtime.permissions_file == Path("/etc/agent/user-permissions.yaml")
    assert config.runtime.profiles["reports"].public_roots == [Path("/mnt/common"), Path("/opt/public")]
    assert config.runtime.workspace_layout == "frontend_user"
    runtime = config.runtime_config(EXAMPLE, "opencode-bucket-0123456789ab")
    assert not hasattr(runtime, "credential_file")
    assert runtime.limits.active == 4


def test_default_values_can_still_be_overridden():
    raw = json.loads(EXAMPLE.read_text())
    raw["control"].update(port=8100)
    raw["control"]["bucket_pool"].update(max_users_per_bucket=8, namespace="test")
    raw["runtime"].update(rpc_port=4107, health_port=8109, limits={"active": 2},
                          opencode={"port": 4106})
    config = FrontendConfig.model_validate(raw)
    assert config.control.port == 8100
    assert config.control.bucket_pool.max_users_per_bucket == 8
    runtime = config.runtime_config(EXAMPLE, "opencode-bucket-0123456789ab")
    assert (runtime.rpc_port, runtime.health_port, runtime.opencode.port) == (4107, 8109, 4106)
    assert runtime.limits.active == 2


def static_config():
    config = load_config(EXAMPLE, FrontendConfig)
    config.control.bucket_pool = None
    config.control.bucket_ids = ["bucket-0"]
    return config


def test_shared_frontend_preserves_callback_and_storage(tmp_path, monkeypatch):
    config = static_config()
    cu = config.control_config()
    runtime = config.runtime_config(EXAMPLE)
    assert list(cu.frontends) == ["web-yk"]
    frontend = cu.frontends["web-yk"]
    assert frontend.credential_file is None
    assert frontend.channel_config["send_msg_url"] == config.send_msg_url
    assert frontend.profile_id in runtime.profiles
    assert runtime.bucket_id in cu.bucket_ids
    assert runtime.storage.prefix == "web-outbound"
    assert runtime.storage.expiry_seconds == 3600
    assert config.instance_idle_ttl_seconds == 3600
    assert runtime.frontend_config_file == EXAMPLE.resolve()

    captured = {}

    def client(service, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("boto3.client", client)
    ArtifactService(runtime, None).client()
    assert captured["aws_access_key_id"] == config.attachment_storage.access_key.get_secret_value()
    assert captured["aws_secret_access_key"] == config.attachment_storage.secret_key.get_secret_value()
    assert captured["aws_secret_access_key"] not in repr(runtime)


def test_kubernetes_bucket_selection():
    config = static_config()
    config.control.bucket_ids.append("bucket-1")
    with pytest.raises(ValueError, match="--bucket-id"):
        config.runtime_config(EXAMPLE)
    runtime = config.runtime_config(EXAMPLE, "bucket-1")
    assert runtime.bucket_id == "bucket-1"
    assert not hasattr(runtime, "credential_file")
    with pytest.raises(ValueError, match="not registered"):
        config.runtime_config(EXAMPLE, "bucket-2")


def test_shared_config_can_live_in_common_but_native_tools_cannot_read_it():
    from agent_backend.config import UserPolicy
    from bucket_runtime.permissions import check_native_paths

    config = static_config()
    config.runtime.profiles[config.profile_id].public_roots = [EXAMPLE.parent]
    runtime = config.runtime_config(EXAMPLE)
    with pytest.raises(PermissionError, match="service configuration"):
        check_native_paths("read", {"filePath": str(EXAMPLE)}, UserPolicy(),
                           runtime.workspace_root, [EXAMPLE.parent], [EXAMPLE])
    with pytest.raises(PermissionError, match="service configuration"):
        check_native_paths("grep", {"path": str(EXAMPLE.parent), "pattern": "secret"}, UserPolicy(),
                           runtime.workspace_root, [EXAMPLE.parent], [EXAMPLE])


def test_no_duplicate_runtime_storage_and_profile_validation():
    raw = json.loads(EXAMPLE.read_text())
    raw["runtime"]["storage"] = raw["attachment_storage"]
    with pytest.raises(ValidationError):
        FrontendConfig.model_validate(raw)
    raw["runtime"].pop("storage")
    raw["profile_id"] = "unknown"
    with pytest.raises(ValidationError, match="runtime.profiles"):
        FrontendConfig.model_validate(raw)


def test_storage_file_credentials_still_supported(tmp_path):
    key = tmp_path / "key"
    key.write_text("file-credential\n")
    storage = StorageConfig(endpoint="http://minio:9000", bucket="attachments",
                            access_key_file=key, secret_key_file=key)
    assert storage.credential("access_key") == "file-credential"
    with pytest.raises(ValidationError, match="exactly one"):
        StorageConfig(endpoint="http://minio:9000", bucket="attachments",
                      access_key="inline", access_key_file=key, secret_key_file=key)


def test_kubernetes_mounts_same_frontend_and_uses_pod_identity():
    import yaml

    manifests = list(yaml.safe_load_all((EXAMPLE.parents[2] / "k8s/opencode/backend.yaml").read_text()))
    workloads = [m for m in manifests if m["kind"] in {"Deployment", "StatefulSet"}]
    for workload in workloads:
        pod = workload["spec"]["template"]["spec"]
        container = pod["containers"][0]
        assert "/mnt/common/frontend.json" in container["args"]
        assert not any(v["name"] == "frontend" for v in pod["volumes"])
        assert next(v for v in pod["volumes"] if v["name"] == "common")["persistentVolumeClaim"]["claimName"] == "opencode-common-pvc"
        if workload["kind"] == "StatefulSet":
            assert container["args"][-2:] == ["--bucket-id", "$(POD_NAME)"]
