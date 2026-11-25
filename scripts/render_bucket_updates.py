"""Render updated bucket manifests without changing Kubernetes or replica counts.

Run from the source root: python -m scripts.render_bucket_updates --config ...
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

from agent_backend.config import FrontendConfig, FrontendsConfig, load_frontends, valid_bucket_id
from container_up.bucket_manager import BucketManager


def render(config: FrontendConfig | FrontendsConfig, current: dict,
           frontend_id: str | None = None) -> dict:
    manager = BucketManager(config.control_config())
    items = []
    for workload in current["items"]:
        metadata = workload["metadata"]
        name = metadata["name"]
        owner = metadata.get("annotations", {}).get("opencode.io/frontend-id")
        if not owner:
            raise ValueError(f"Deployment is missing its frontend_id annotation: {name}")
        if (metadata.get("namespace") != config.control.bucket_pool.namespace
                or metadata.get("labels", {}).get("bucket-id") != name
                or not valid_bucket_id(config.control.bucket_pool.name_prefix, owner, name)):
            raise ValueError(f"Deployment is not owned by this bucket pool: {name}")
        if owner and frontend_id and owner != frontend_id:
            continue
        env = workload["spec"]["template"]["spec"]["containers"][0].get("env", [])
        password = next((entry.get("value") for entry in env if entry["name"] == "OPENCODE_PASSWORD"), None)
        if not password:
            raise ValueError("Native bucket credentials missing; complete the coordinated upgrade first")
        desired = manager.deployment(name, owner, password)
        desired["spec"]["replicas"] = workload["spec"].get("replicas", 1)
        items.append(desired)
    return {"apiVersion": "v1", "kind": "List", "items": items}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--frontend-id", help="Only update buckets belonging to this frontend")
    args = parser.parse_args()
    config = load_frontends(args.config)
    result = subprocess.run(["kubectl", "-n", config.control.bucket_pool.namespace,
        "get", "deployments", "-l", "app=opencode-bucket", "-o", "json"],
        check=True, capture_output=True, text=True)
    yaml.safe_dump(render(config, json.loads(result.stdout), args.frontend_id), sys.stdout, sort_keys=False)


if __name__ == "__main__":
    main()
