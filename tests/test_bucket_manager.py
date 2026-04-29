from __future__ import annotations

import pytest
from fastapi import HTTPException

from container_up.app import BridgeOutboundRequest, OutboundRequest, outbound, post_bridge_outbound
from container_up.bucket_manager import BucketManager
from container_up.settings import build_bucket_base_url


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
    assert "BUCKET_MOUNT_ROOT" in env_names
    assert "BUCKET_MOUNT_PVC" in env_names
    assert "SOURCE_ROOT" in env_names
    assert "SOURCE_PVC" in env_names
    assert "OUTBOUND_GATEWAY_URL" in env_names
    assert "SKILLS_ROOT" not in env_names
    assert "TEMPLATES_ROOT" not in env_names

    volume_mounts = manifest["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert volume_mounts == [
        {"name": "bucket-mount-root", "mountPath": "/mnt/nanobot"},
        {"name": "source-root", "mountPath": "/mnt/nanobot/source", "readOnly": True},
    ]


def test_build_bucket_base_url_uses_namespace_short_name() -> None:
    assert build_bucket_base_url("bucket-5", "nanobot-bucket-5") == "http://nanobot-bucket-5.nanobot:8080"


@pytest.mark.asyncio
async def test_outbound_returns_http_503_on_delivery_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_forward(_packet):
        raise RuntimeError("delivery failed")

    monkeypatch.setattr("container_up.app._forward_outbound_message", fake_forward)

    with pytest.raises(HTTPException) as exc_info:
        await outbound(
            OutboundRequest(
                frontend_id="feishu-main",
                user_id="user-1",
                chat_id="conv-1",
                content="hello",
            )
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "delivery failed"


@pytest.mark.asyncio
async def test_bridge_outbound_returns_http_503_on_delivery_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_forward(_packet):
        raise RuntimeError("delivery failed")

    monkeypatch.setattr("container_up.app._forward_outbound_message", fake_forward)

    with pytest.raises(HTTPException) as exc_info:
        await post_bridge_outbound(
            BridgeOutboundRequest(
                frontend_id="feishu-main",
                to="conv-1",
                content="hello",
            )
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "delivery failed"
