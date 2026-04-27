from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import time
from typing import Any

import httpx

from container_up.settings import (
    BUCKET_COMMON_MOUNT_PATH,
    BUCKET_COMMON_PVC,
    BUCKET_CONTAINER_PORT,
    BUCKET_CREATE_COMMAND_TEMPLATE,
    BUCKET_FRONTENDS_CONFIG_PATH,
    BUCKET_FRONTENDS_MOUNT_PATH,
    BUCKET_FRONTENDS_PVC,
    BUCKET_IMAGE_PULL_POLICY,
    BUCKET_INSTANCE_EVICT_INTERVAL_SECONDS,
    BUCKET_INSTANCE_IDLE_TTL_SECONDS,
    BUCKET_INSTANCE_STOP_GRACE_SECONDS,
    BUCKET_KUBECTL_BIN,
    BUCKET_MAX_PROCESSES,
    BUCKET_NANOBOT_PORT_END,
    BUCKET_NANOBOT_PORT_START,
    BUCKET_RUNTIME_IMAGE,
    BUCKET_SERVICE_PORT,
    BUCKET_SKILLS_ROOT,
    BUCKET_SOURCE_PVC,
    BUCKET_SOURCE_ROOT,
    BUCKET_TEMPLATES_ROOT,
    BUCKET_READY_TIMEOUT,
    BUCKET_REQUEST_TIMEOUT,
    BUCKET_SKIP_HEALTHCHECK,
    BUCKET_WORKSPACES_MOUNT_PATH,
    BUCKET_WORKSPACES_PVC,
)


class BucketManager:
    def __init__(self) -> None:
        self._ensure_locks: dict[str, asyncio.Lock] = {}
        self._ensured_buckets: set[str] = set()

    async def ensure_bucket_exists(self, bucket: dict[str, Any]) -> None:
        bucket_id = str(bucket["bucket_id"])
        lock = self._ensure_locks.setdefault(bucket_id, asyncio.Lock())
        async with lock:
            if BUCKET_CREATE_COMMAND_TEMPLATE:
                if bucket_id in self._ensured_buckets:
                    return
                command = BUCKET_CREATE_COMMAND_TEMPLATE.format(
                    bucket_id=bucket_id,
                    bucket_name=str(bucket["bucket_name"]),
                    namespace=str(bucket["namespace"]),
                    service_host=str(bucket["service_host"] or ""),
                    service_port=str(bucket["service_port"] or ""),
                )
                await asyncio.to_thread(_run_command, command)
            else:
                await asyncio.to_thread(self._apply_bucket_resources, bucket)
            self._ensured_buckets.add(bucket_id)

    async def wait_bucket_ready(self, bucket: dict[str, Any], timeout: float = BUCKET_READY_TIMEOUT) -> None:
        if BUCKET_SKIP_HEALTHCHECK:
            return

        deadline = time.monotonic() + timeout
        url = f"{str(bucket['service_host']).rstrip('/')}/health/ready"
        async with httpx.AsyncClient(timeout=BUCKET_REQUEST_TIMEOUT) as client:
            while time.monotonic() < deadline:
                try:
                    response = await client.get(url)
                    if response.status_code < 400:
                        return
                except Exception:
                    pass
                await asyncio.sleep(1)
        raise RuntimeError(f"bucket did not become ready: {bucket['bucket_id']}")

    def get_bucket_url(self, bucket: dict[str, Any]) -> str:
        return str(bucket["service_host"]).rstrip("/")

    async def scale_bucket_to_zero(self, bucket: dict[str, Any]) -> None:
        if BUCKET_CREATE_COMMAND_TEMPLATE:
            return
        await asyncio.to_thread(self._scale_deployment, bucket, 0)

    def _apply_bucket_resources(self, bucket: dict[str, Any]) -> None:
        service_manifest = self._build_service_manifest(bucket)
        deployment_manifest = self._build_deployment_manifest(bucket)
        _kubectl_apply(service_manifest)
        _kubectl_apply(deployment_manifest)

    def _scale_deployment(self, bucket: dict[str, Any], replicas: int) -> None:
        subprocess.run(
            [
                BUCKET_KUBECTL_BIN,
                "-n",
                str(bucket["namespace"]),
                "scale",
                f"deployment/{bucket['bucket_name']}",
                f"--replicas={replicas}",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @staticmethod
    def _build_service_manifest(bucket: dict[str, Any]) -> dict[str, Any]:
        bucket_id = str(bucket["bucket_id"])
        bucket_name = str(bucket["bucket_name"])
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": bucket_name,
                "namespace": str(bucket["namespace"]),
                "labels": {
                    "app": "nanobot-bucket",
                    "bucket-id": bucket_id,
                },
            },
            "spec": {
                "selector": {
                    "app": "nanobot-bucket",
                    "bucket-id": bucket_id,
                },
                "ports": [
                    {
                        "name": "http",
                        "port": BUCKET_SERVICE_PORT,
                        "targetPort": BUCKET_CONTAINER_PORT,
                    }
                ],
            },
        }

    @staticmethod
    def _build_deployment_manifest(bucket: dict[str, Any]) -> dict[str, Any]:
        bucket_id = str(bucket["bucket_id"])
        bucket_name = str(bucket["bucket_name"])
        env = [
            {"name": "BUCKET_RUNTIME_PORT", "value": str(BUCKET_CONTAINER_PORT)},
            {"name": "BUCKET_ID", "value": bucket_id},
            {"name": "FRONTENDS_CONFIG_PATH", "value": BUCKET_FRONTENDS_CONFIG_PATH},
            {"name": "SOURCE_ROOT", "value": BUCKET_SOURCE_ROOT},
            {"name": "SKILLS_ROOT", "value": BUCKET_SKILLS_ROOT},
            {"name": "TEMPLATES_ROOT", "value": BUCKET_TEMPLATES_ROOT},
            {"name": "WORKSPACE_ROOT", "value": BUCKET_WORKSPACES_MOUNT_PATH},
            {"name": "CHILD_WORKSPACE_TARGET", "value": BUCKET_WORKSPACES_MOUNT_PATH},
            {"name": "HOST_WORKSPACE_ROOT", "value": BUCKET_WORKSPACES_MOUNT_PATH},
            {
                "name": "OUTBOUND_GATEWAY_URL",
                "value": "http://container-up.nanobot.svc.cluster.local:8080/outbound",
            },
            {"name": "INSTANCE_IDLE_TTL_SECONDS", "value": str(BUCKET_INSTANCE_IDLE_TTL_SECONDS)},
            {
                "name": "INSTANCE_STOP_GRACE_SECONDS",
                "value": str(BUCKET_INSTANCE_STOP_GRACE_SECONDS),
            },
            {
                "name": "INSTANCE_EVICT_INTERVAL_SECONDS",
                "value": str(BUCKET_INSTANCE_EVICT_INTERVAL_SECONDS),
            },
            {"name": "MAX_PROCESSES_PER_BUCKET", "value": str(BUCKET_MAX_PROCESSES)},
            {"name": "NANOBOT_PORT_START", "value": str(BUCKET_NANOBOT_PORT_START)},
            {"name": "NANOBOT_PORT_END", "value": str(BUCKET_NANOBOT_PORT_END)},
        ]
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": bucket_name,
                "namespace": str(bucket["namespace"]),
                "labels": {
                    "app": "nanobot-bucket",
                    "bucket-id": bucket_id,
                },
            },
            "spec": {
                "replicas": 1,
                "selector": {
                    "matchLabels": {
                        "app": "nanobot-bucket",
                        "bucket-id": bucket_id,
                    }
                },
                "template": {
                    "metadata": {
                        "labels": {
                            "app": "nanobot-bucket",
                            "bucket-id": bucket_id,
                        }
                    },
                    "spec": {
                        "containers": [
                            {
                                "name": "bucket-runtime",
                                "image": BUCKET_RUNTIME_IMAGE,
                                "imagePullPolicy": BUCKET_IMAGE_PULL_POLICY,
                                "command": ["python", "-m", "bucket_runtime.main"],
                                "ports": [{"containerPort": BUCKET_CONTAINER_PORT}],
                                "env": env,
                                "volumeMounts": [
                                    {
                                        "name": "source",
                                        "mountPath": BUCKET_SOURCE_ROOT,
                                        "readOnly": True,
                                    },
                                    {
                                        "name": "common",
                                        "mountPath": BUCKET_COMMON_MOUNT_PATH,
                                        "readOnly": True,
                                    },
                                    {
                                        "name": "frontends",
                                        "mountPath": BUCKET_FRONTENDS_MOUNT_PATH,
                                        "readOnly": True,
                                    },
                                    {
                                        "name": "workspaces",
                                        "mountPath": BUCKET_WORKSPACES_MOUNT_PATH,
                                    },
                                ],
                                "readinessProbe": {
                                    "httpGet": {
                                        "path": "/health/ready",
                                        "port": BUCKET_CONTAINER_PORT,
                                    },
                                    "initialDelaySeconds": 5,
                                    "periodSeconds": 5,
                                },
                                "livenessProbe": {
                                    "httpGet": {
                                        "path": "/health/live",
                                        "port": BUCKET_CONTAINER_PORT,
                                    },
                                    "initialDelaySeconds": 20,
                                    "periodSeconds": 10,
                                },
                            }
                        ],
                        "volumes": [
                            {
                                "name": "source",
                                "persistentVolumeClaim": {"claimName": BUCKET_SOURCE_PVC},
                            },
                            {
                                "name": "common",
                                "persistentVolumeClaim": {"claimName": BUCKET_COMMON_PVC},
                            },
                            {
                                "name": "frontends",
                                "persistentVolumeClaim": {"claimName": BUCKET_FRONTENDS_PVC},
                            },
                            {
                                "name": "workspaces",
                                "persistentVolumeClaim": {"claimName": BUCKET_WORKSPACES_PVC},
                            },
                        ],
                    },
                },
            },
        }


def _run_command(command: str) -> None:
    subprocess.run(shlex.split(command), check=True)


def _kubectl_apply(manifest: dict[str, Any]) -> None:
    subprocess.run(
        [BUCKET_KUBECTL_BIN, "apply", "-f", "-"],
        input=json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
