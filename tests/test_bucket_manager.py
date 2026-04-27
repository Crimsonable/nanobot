from __future__ import annotations

from container_up.bucket_manager import BucketManager


def test_bucket_manager_builds_service_manifest() -> None:
    bucket = {
        "bucket_id": "bucket-3",
        "bucket_name": "nanobot-bucket-3",
        "namespace": "nanobot",
    }

    manifest = BucketManager._build_service_manifest(bucket)

    assert manifest["kind"] == "Service"
    assert manifest["metadata"]["name"] == "nanobot-bucket-3"
    assert manifest["metadata"]["labels"]["bucket-id"] == "bucket-3"
    assert manifest["spec"]["selector"]["bucket-id"] == "bucket-3"


def test_bucket_manager_builds_deployment_manifest() -> None:
    bucket = {
        "bucket_id": "bucket-5",
        "bucket_name": "nanobot-bucket-5",
        "namespace": "nanobot",
    }

    manifest = BucketManager._build_deployment_manifest(bucket)

    assert manifest["kind"] == "Deployment"
    assert manifest["metadata"]["name"] == "nanobot-bucket-5"
    assert manifest["spec"]["replicas"] == 1
    labels = manifest["spec"]["template"]["metadata"]["labels"]
    assert labels["bucket-id"] == "bucket-5"
    env_names = {item["name"] for item in manifest["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "BUCKET_ID" in env_names
    assert "OUTBOUND_GATEWAY_URL" in env_names
