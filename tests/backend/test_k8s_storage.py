from pathlib import Path

import yaml

from agent_backend.config import FrontendConfig, load_config
from container_up.bucket_manager import BucketManager

ROOT = Path(__file__).resolve().parents[2]


def test_original_pvc_mounts_match_control_and_dynamic_buckets():
    config = load_config(ROOT / "config/opencode/frontend.json", FrontendConfig)
    docs = list(yaml.safe_load_all((ROOT / "k8s/opencode/backend.yaml").read_text()))
    control = next(d for d in docs if d["kind"] == "Deployment")
    runtime = BucketManager(config.control_config()).deployment("opencode-bucket-0123456789ab")
    for deployment in [control, runtime]:
        pod = deployment["spec"]["template"]["spec"]
        volumes = {v["name"]: v for v in pod["volumes"]}
        mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
        for path, claim, readonly in [("/mnt/env", "opencode-data-pvc", False),
                                      ("/mnt/source", "opencode-source-pvc", True),
                                      ("/mnt/common", "opencode-common-pvc", True)]:
            mount = mounts[path]
            assert mount.get("readOnly", False) == readonly
            assert volumes[mount["name"]]["persistentVolumeClaim"]["claimName"] == claim
        assert volumes["data"]["persistentVolumeClaim"]["claimName"] == "opencode-state-pvc"
    assert Path("/mnt/common") in config.runtime.profiles[config.profile_id].public_roots
    assert config.runtime.workspace_root == Path("/mnt/env/workspaces")
    assert not any(d["kind"] == "StatefulSet" for d in docs)
    assert len([d for d in docs if d["kind"] == "Deployment"]) == 1


def test_local_nfs_selector_and_retained_old_server_paths():
    source = (ROOT / "k8s/opencode/storage.yaml").read_text()
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
            assert storage["path"] == f"/data/nanobot_backend_k8s/{name}"
        assert "hostPath" in volumes["opencode-state-pv-local"]["spec"]
