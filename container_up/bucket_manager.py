"""Kubernetes workload lifecycle; CU WebSocket carries bucket control traffic."""
from __future__ import annotations

import asyncio
import ssl
from pathlib import PurePosixPath

import httpx

from agent_backend.common import frontend_segment
from agent_backend.config import ControlConfig
from agent_backend.models import digest
from agent_backend.workspaces import workspace_volume_subpath


class BucketManager:
    _WRITE_ATTEMPTS = 5

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

    def deployment(self, bucket_id: str, frontend_id: str) -> dict:
        if frontend_id not in self.config.frontends:
            raise ValueError("bucket requires a registered frontend_id")
        frontend_id = frontend_segment(frontend_id)
        pool, runtime = self.pool, self.config.runtime_for(frontend_id)
        workspace_subpath = workspace_volume_subpath(runtime.workspace_root, frontend_id)
        labels = {"app": "opencode-bucket", "bucket-id": bucket_id}
        pod_labels = {**labels, "frontend-key": digest(frontend_id)[:16]}
        annotations = {"opencode.io/frontend-id": frontend_id}
        container = {
            "name": "runtime", "workingDir": "/mnt/source", "image": pool.image, "imagePullPolicy": pool.image_pull_policy,
            "args": ["python", "-m", "bucket_runtime", "--config",
                     self.config.frontend_config_path.as_posix(), "--frontend-id", frontend_id,
                     "--bucket-id", bucket_id],
            "securityContext": {"privileged": True, "allowPrivilegeEscalation": True,
                                "readOnlyRootFilesystem": False},
            "resources": {"requests": {"cpu": "1", "memory": "1Gi"},
                          "limits": {"cpu": "4", "memory": "4Gi"}},
            "volumeMounts": [
                {"name": "data", "mountPath": "/data"},
                {"name": "workspace", "mountPath": str(PurePosixPath("/mnt/env") / workspace_subpath),
                 "subPath": str(workspace_subpath)},
                {"name": "source", "mountPath": "/mnt/source", "readOnly": True},
                {"name": "opencode", "mountPath": "/opt/opencode", "readOnly": True},
                {"name": "common", "mountPath": str(PurePosixPath(runtime.common_root.as_posix()) / frontend_id / "app"),
                 "subPath": frontend_id + "/app", "readOnly": True},
                {"name": "common", "mountPath": str(PurePosixPath(runtime.common_root.as_posix()) / frontend_id / "config"),
                 "subPath": frontend_id + "/config", "readOnly": True},
                {"name": "common", "mountPath": self.config.frontend_config_path.as_posix(),
                 "subPath": self.config.frontend_config_path.name, "readOnly": True},
                {"name": "tmp", "mountPath": "/tmp"}],
            "startupProbe": {"httpGet": {"path": "/health/ready", "port": runtime.health_port},
                             "failureThreshold": max(30, pool.ready_timeout_seconds // 5), "periodSeconds": 5},
            "readinessProbe": {"httpGet": {"path": "/health/ready", "port": runtime.health_port}},
            "livenessProbe": {"httpGet": {"path": "/health/live", "port": runtime.health_port}},
        }
        return {"apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": bucket_id, "namespace": pool.namespace, "labels": pod_labels,
                             "annotations": annotations},
                "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                         "selector": {"matchLabels": labels}, "template": {
                             "metadata": {"labels": pod_labels, "annotations": annotations}, "spec": {
                                 "automountServiceAccountToken": False,
                                 "nodeSelector": pool.node_selector,
                                 "terminationGracePeriodSeconds": 45,
                                 "securityContext": {"runAsUser": 0, "runAsGroup": 0, "fsGroup": 0},
                                 "containers": [container], "volumes": [
                                     {"name": "data", "persistentVolumeClaim": {"claimName": pool.state_pvc}},
                                     {"name": "workspace", "persistentVolumeClaim": {"claimName": pool.data_pvc}},
                                     {"name": "source", "persistentVolumeClaim": {"claimName": pool.source_pvc, "readOnly": True}},
                                     {"name": "common", "persistentVolumeClaim": {"claimName": pool.common_pvc, "readOnly": True}},
                                     {"name": "opencode", "hostPath": {"path": pool.opencode_host_path, "type": "Directory"}},
                                     {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
                                 ]}}}}

    async def ensure(self, bucket_id: str, frontend_id: str) -> None:
        collection = f"/apis/apps/v1/namespaces/{self.pool.namespace}/deployments"
        for attempt in range(self._WRITE_ATTEMPTS):
            response = await self.request("GET", collection + "/" + bucket_id)
            if response.status_code == 404:
                response = await self.request("POST", collection, json=self.deployment(bucket_id, frontend_id))
                if response.status_code == 409:
                    response = await self.request("GET", collection + "/" + bucket_id)
            response.raise_for_status()
            current = response.json()
            if current.get("metadata", {}).get("labels", {}).get("bucket-id") != bucket_id:
                raise RuntimeError("existing Deployment is not owned by this bucket")
            owner = current.get("metadata", {}).get("annotations", {}).get("opencode.io/frontend-id")
            if owner != frontend_id:
                raise RuntimeError("existing Deployment belongs to another frontend")
            desired = self.deployment(bucket_id, frontend_id)
            desired["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
            response = await self.request("PUT", collection + "/" + bucket_id, json=desired)
            if not await self._retry_conflict(response, attempt):
                break
        await self.scale(bucket_id, 1)

    async def _retry_conflict(self, response: httpx.Response, attempt: int) -> bool:
        if response.status_code == 409 and attempt + 1 < self._WRITE_ATTEMPTS:
            # Re-read and rebuild the write on the next attempt; stale RVs cannot be replayed.
            await asyncio.sleep(0.1 * 2 ** attempt)
            return True
        response.raise_for_status()
        return False

    async def scale(self, bucket_id: str, replicas: int) -> None:
        path = f"/apis/apps/v1/namespaces/{self.pool.namespace}/deployments/{bucket_id}/scale"
        for attempt in range(self._WRITE_ATTEMPTS):
            response = await self.request("GET", path)
            if response.status_code == 404 and replicas == 0:
                return
            response.raise_for_status()
            scale = response.json()
            scale["spec"]["replicas"] = replicas
            response = await self.request("PUT", path, json=scale)
            if not await self._retry_conflict(response, attempt):
                break
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
