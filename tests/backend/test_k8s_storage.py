from pathlib import Path
import json

import yaml

from agent_backend.config import load_frontends
from container_up.bucket_manager import BucketManager

ROOT = Path(__file__).resolve().parents[2]


def test_shared_instructions_use_the_bucket_container_mount():
    config = load_frontends(ROOT / "config/opencode/frontends.json").frontend("web-yk")
    native = json.loads((ROOT / "config/opencode/web-yk/config/opencode-server.json").read_text(encoding="utf-8"))
    assert native["instructions"] == ["/mnt/common/web-yk/app/templates/AGENTS.md"]
    deployment = BucketManager(config.control_config()).deployment("opencode-bucket-0123456789ab", config.frontend_id)
    mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert any(m["mountPath"] == "/mnt/common/web-yk/app" and m["subPath"] == "web-yk/app"
               and m["readOnly"] for m in mounts)


def test_all_services_use_one_source_mount_and_shared_opencode_binary():
    config = load_frontends(ROOT / "config/opencode/frontends.json").frontend("web-yk")
    workloads = []
    for path in ["k8s/opencode/backend.yaml", "k8s/opencode/engineering-scene-mcp.yaml", "k8s/opencode/hazard-expert-mcp.yaml", "k8s/opencode/web-server.yaml"]:
        workloads.extend(d for d in yaml.safe_load_all((ROOT / path).read_text(encoding="utf-8")) if d["kind"] == "Deployment")
    runtime = BucketManager(config.control_config()).deployment("opencode-bucket-0123456789ab", config.frontend_id)
    workloads.append(runtime)
    for deployment in workloads:
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]
        mounts = container["volumeMounts"]
        source = next(v for v in pod["volumes"] if v.get("persistentVolumeClaim", {}).get("claimName") == "opencode-source-pvc")
        target = "/mnt/source"
        assert any(m["name"] == source["name"] and m["mountPath"] == target and m["readOnly"] for m in mounts)
        assert container["workingDir"].startswith(target)
        assert pod["nodeSelector"] == {"opencode-runtime": "true"}
        assert len([m for m in mounts if m["name"] == source["name"]]) == 1
        assert not any(m["mountPath"] in {"/opt/app", "/opt/public", "/opt/opencode-plugin"} for m in mounts)
    mounts = runtime["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert any(m["mountPath"] == "/opt/opencode" and m["readOnly"] for m in mounts)
    assert config.runtime.opencode.plugin_file == Path("/mnt/source/opencode_plugin/dist/index.mjs")
    assert config.runtime.opencode.dependency_dir == Path("/mnt/source/opencode_plugin")
    for name in ["Dockerfile.container_up", "Dockerfile.bucket_runtime", "Dockerfile.web_server"]:
        docker = (ROOT / name).read_text(encoding="utf-8")
        assert not any(line.startswith(("COPY agent_backend ", "COPY container_up ", "COPY bucket_runtime ",
                                       "COPY web_server ", "COPY public ")) for line in docker.splitlines())
    docker = (ROOT / "Dockerfile.bucket_runtime").read_text(encoding="utf-8")
    assert "COPY vendor/plugin" not in docker and "COPY vendor/opencode" not in docker
    other = BucketManager(config.control_config()).deployment("opencode-bucket-abcdef123456", config.frontend_id)
    for workload in [runtime, other]:
        volumes = {v["name"]: v for v in workload["spec"]["template"]["spec"]["volumes"]}
        assert volumes["opencode"]["hostPath"] == {
            "path": "/data/opencode_backend_k8s/opencode", "type": "Directory"}
    config.control.bucket_pool.opencode_host_path = "/custom/opencode"
    custom = BucketManager(config.control_config()).deployment("opencode-bucket-0123456789ab", config.frontend_id)
    volume = next(v for v in custom["spec"]["template"]["spec"]["volumes"] if v["name"] == "opencode")
    assert volume["hostPath"]["path"] == "/custom/opencode"
    native = yaml.safe_load((ROOT / "config/opencode/web-yk/config/opencode-server.json").read_text(encoding="utf-8"))
    assert native["autoupdate"] is False
    assert native["skills"]["paths"] == ["/mnt/common/web-yk/app/skills"]
    assert "COPY --chown=mcp:mcp hazard_expert_mcp" not in (ROOT / "hazard_expert_mcp/Dockerfile").read_text(encoding="utf-8")


def test_original_pvc_mounts_match_control_and_dynamic_buckets():
    config = load_frontends(ROOT / "config/opencode/frontends.json").frontend("web-yk")
    docs = list(yaml.safe_load_all((ROOT / "k8s/opencode/backend.yaml").read_text(encoding="utf-8")))
    control = next(d for d in docs if d["kind"] == "Deployment")
    runtime = BucketManager(config.control_config()).deployment("opencode-bucket-0123456789ab", config.frontend_id)
    for deployment in [control, runtime]:
        pod = deployment["spec"]["template"]["spec"]
        volumes = {v["name"]: v for v in pod["volumes"]}
        mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
        for path, claim, readonly in [("/mnt/env" if deployment is control else "/mnt/env/workspaces/web-yk", "opencode-data-pvc", False),
                                      ("/mnt/source", "opencode-source-pvc", True),
                                      ("/mnt/common" if deployment is control else "/mnt/common/web-yk/app", "opencode-common-pvc", True)]:
            mount = mounts[path]
            assert mount.get("readOnly", False) == readonly
            assert volumes[mount["name"]]["persistentVolumeClaim"]["claimName"] == claim
        assert volumes["data"]["persistentVolumeClaim"]["claimName"] == "opencode-state-pvc"
    mounts = {m["mountPath"]: m for m in runtime["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]}
    assert "/mnt/common" not in mounts
    assert "/mnt/env" not in mounts
    assert "/mnt/env/workspaces" not in mounts
    assert mounts["/mnt/env/workspaces/web-yk"]["subPath"] == "workspaces/web-yk"
    assert "/mnt/common/web-yk" not in mounts
    assert mounts["/mnt/common/web-yk/app"]["subPath"] == "web-yk/app"
    assert mounts["/mnt/common/web-yk/config"]["subPath"] == "web-yk/config"
    assert mounts["/mnt/common/web-yk/config"]["readOnly"] is True
    assert mounts["/mnt/common/frontends.json"]["subPath"] == "frontends.json"
    config.frontend_id = "qxt-main"
    other = BucketManager(config.control_config()).deployment("opencode-bucket-abcdef123456", config.frontend_id)
    mounts = other["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert {m["subPath"] for m in mounts if m["name"] == "common"} == {"qxt-main/app", "qxt-main/config", "frontends.json"}
    assert [m for m in mounts if m["name"] == "workspace"] == [{
        "name": "workspace", "mountPath": "/mnt/env/workspaces/qxt-main", "subPath": "workspaces/qxt-main"}]
    assert config.runtime.workspace_root == Path("/mnt/env/workspaces")
    assert not any(d["kind"] == "StatefulSet" for d in docs)
    assert len([d for d in docs if d["kind"] == "Deployment"]) == 1


def test_frontend_configuration_uses_common_pvc_without_global_credentials():
    config = load_frontends(ROOT / "config/opencode/frontends.json").frontend("web-yk")
    docs = list(yaml.safe_load_all((ROOT / "k8s/opencode/backend.yaml").read_text(encoding="utf-8")))
    control = next(d for d in docs if d["kind"] == "Deployment")
    runtime = BucketManager(config.control_config()).deployment("opencode-bucket-0123456789ab", config.frontend_id)
    for deployment in [control, runtime]:
        pod = deployment["spec"]["template"]["spec"]
        assert not any(v["name"] in {"config", "secrets"} for v in pod["volumes"])
        assert not any(m["mountPath"] in {"/etc/agent", "/run/secrets"}
                       for m in pod["containers"][0]["volumeMounts"])
        assert pod["automountServiceAccountToken"] is (deployment is control)
    cfg = config.runtime_config(ROOT / "config/opencode/frontends.json", "opencode-bucket-0123456789ab")
    assert cfg.opencode.config_file == Path("/mnt/common/web-yk/config/opencode-server.json")
    assert cfg.permissions_file == Path("/mnt/common/web-yk/config/user-permissions.yaml")


def test_local_nfs_selector_and_unified_server_paths():
    source = (ROOT / "k8s/opencode/storage.yaml").read_text(encoding="utf-8")
    for backend in ["local", "nfs"]:
        manifest = yaml.safe_load(source.replace("&storage_backend local", "&storage_backend " + backend))
        volumes = {v["metadata"]["name"]: v for v in manifest["items"] if v["kind"] == "PersistentVolume"}
        claims = {v["metadata"]["name"]: v for v in manifest["items"] if v["kind"] == "PersistentVolumeClaim"}
        for name in ["data", "source", "common"]:
            labels = claims[f"opencode-{name}-pvc"]["spec"]["selector"]["matchLabels"]
            assert labels["opencode.io/storage-backend"] == backend
            pv = volumes[f"opencode-{name}-pv-{backend}"]
            assert pv["spec"]["persistentVolumeReclaimPolicy"] == "Retain"
            storage = pv["spec"]["hostPath" if backend == "local" else "nfs"]
            assert storage["path"] == f"/data/opencode_backend_k8s/{name}"
        assert "hostPath" in volumes["opencode-state-pv-local"]["spec"]


def test_bucket_upgrade_preserves_sleeping_and_running_replica_counts():
    from scripts.render_bucket_updates import render

    config = load_frontends(ROOT / "config/opencode/frontends.json").frontend("web-yk")
    manager = BucketManager(config.control_config())
    current = [manager.deployment("opencode-bucket-0123456789ab", config.frontend_id),
               manager.deployment("opencode-bucket-abcdef012345", config.frontend_id)]
    current[0]["spec"]["replicas"] = 0
    for workload in current:
        workload["spec"]["template"]["spec"]["containers"][0]["workingDir"] = "/opt/app"
    output = render(config, {"items": current})
    assert [w["spec"]["replicas"] for w in output["items"]] == [0, 1]
    assert all(w["spec"]["template"]["spec"]["containers"][0]["workingDir"] == "/mnt/source"
               for w in output["items"])
    current[0]["metadata"]["labels"]["bucket-id"] = "another-bucket"
    import pytest
    with pytest.raises(ValueError, match="not owned"):
        render(config, {"items": current})
