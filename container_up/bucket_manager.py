"""Kubernetes workload lifecycle. All runtime traffic still uses the CU WebSocket."""
from __future__ import annotations

import asyncio
import ssl

import httpx

from agent_backend.config import ControlConfig


class BucketManager:
    def __init__(self, config: ControlConfig):
        self.config = config
        self.pool = config.bucket_pool

    async def request(self, method: str, path: str, **kwargs):
        # Read the projected token on every call so Kubernetes token rotation works.
        context = ssl.create_default_context(cafile=str(self.pool.ca_file))
        async with httpx.AsyncClient(base_url=self.pool.api_url, verify=context, timeout=30,
                                     trust_env=False) as client:
            response = await client.request(method, path, headers={
                "Authorization": "Bearer " + self.pool.token_file.read_text().strip()}, **kwargs)
        if response.status_code not in {404, 409}:
            response.raise_for_status()
        return response

    def deployment(self, bucket_id: str) -> dict:
        pool, runtime = self.pool, self.config.runtime_template
        labels = {"app": "opencode-bucket", "bucket-id": bucket_id}
        container = {
            "name": "runtime", "image": pool.image, "imagePullPolicy": pool.image_pull_policy,
            "args": ["python", "-m", "agent_backend", "runtime", "--config",
                     "/mnt/common/frontend.json", "--bucket-id", bucket_id],
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "1", "memory": "1Gi"},
                          "limits": {"cpu": "4", "memory": "4Gi"}},
            "volumeMounts": [
                {"name": "data", "mountPath": "/data"},
                {"name": "workspace", "mountPath": "/mnt/env"},
                {"name": "source", "mountPath": "/mnt/source", "readOnly": True},
                {"name": "common", "mountPath": "/mnt/common", "readOnly": True},
                {"name": "config", "mountPath": "/etc/agent", "readOnly": True},
                {"name": "secrets", "mountPath": "/run/secrets", "readOnly": True},
                {"name": "tmp", "mountPath": "/tmp"}],
            "startupProbe": {"httpGet": {"path": "/health/ready", "port": runtime.health_port},
                             "failureThreshold": max(30, pool.ready_timeout_seconds // 5), "periodSeconds": 5},
            "readinessProbe": {"httpGet": {"path": "/health/ready", "port": runtime.health_port}},
            "livenessProbe": {"httpGet": {"path": "/health/live", "port": runtime.health_port}},
        }
        return {"apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": bucket_id, "namespace": pool.namespace, "labels": labels},
                "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                         "selector": {"matchLabels": labels}, "template": {
                             "metadata": {"labels": labels}, "spec": {
                                 "automountServiceAccountToken": False,
                                 "nodeSelector": pool.node_selector,
                                 "terminationGracePeriodSeconds": 45,
                                 "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001},
                                 "containers": [container], "volumes": [
                                     {"name": "data", "persistentVolumeClaim": {"claimName": pool.state_pvc}},
                                     {"name": "workspace", "persistentVolumeClaim": {"claimName": pool.data_pvc}},
                                     {"name": "source", "persistentVolumeClaim": {"claimName": pool.source_pvc, "readOnly": True}},
                                     {"name": "common", "persistentVolumeClaim": {"claimName": pool.common_pvc, "readOnly": True}},
                                     {"name": "config", "configMap": {"name": pool.config_map}},
                                     {"name": "secrets", "secret": {"secretName": pool.runtime_secret}},
                                     {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
                                 ]}}}}

    async def ensure(self, bucket_id: str) -> None:
        collection = f"/apis/apps/v1/namespaces/{self.pool.namespace}/deployments"
        response = await self.request("GET", collection + "/" + bucket_id)
        if response.status_code == 404:
            response = await self.request("POST", collection, json=self.deployment(bucket_id))
            if response.status_code != 409:
                response.raise_for_status()
            else:
                response = await self.request("GET", collection + "/" + bucket_id)
        response.raise_for_status()
        current = response.json()
        if current.get("metadata", {}).get("labels", {}).get("bucket-id") != bucket_id:
            raise RuntimeError("existing Deployment is not owned by this bucket")
        desired = self.deployment(bucket_id)
        desired["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
        response = await self.request("PUT", collection + "/" + bucket_id, json=desired)
        response.raise_for_status()
        await self.scale(bucket_id, 1)

    async def scale(self, bucket_id: str, replicas: int) -> None:
        path = f"/apis/apps/v1/namespaces/{self.pool.namespace}/deployments/{bucket_id}/scale"
        response = await self.request("GET", path)
        if response.status_code == 404 and replicas == 0:
            return
        response.raise_for_status()
        scale = response.json()
        scale["spec"]["replicas"] = replicas
        response = await self.request("PUT", path, json=scale)
        response.raise_for_status()
        if replicas == 0:
            # Do not reuse its ID/state directory while the old writer is still terminating.
            async with asyncio.timeout(self.pool.ready_timeout_seconds):
                while True:
                    response = await self.request("GET", path.removesuffix("/scale"))
                    if response.status_code == 404:
                        return
                    response.raise_for_status()
                    if response.json().get("status", {}).get("replicas", 0) == 0:
                        pods = await self.request("GET", f"/api/v1/namespaces/{self.pool.namespace}/pods",
                                                  params={"labelSelector": f"app=opencode-bucket,bucket-id={bucket_id}"})
                        pods.raise_for_status()
                        if not pods.json().get("items"):
                            return
                    await asyncio.sleep(0.5)
