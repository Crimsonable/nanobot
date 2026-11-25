"""Kubernetes lifecycle for containers running the native OpenCode server."""
from __future__ import annotations

import asyncio
import base64
import json
import ssl
from pathlib import PurePosixPath

import httpx

from agent_backend.common import frontend_segment
from agent_backend.config import BucketConfig, ControlConfig
from agent_backend.models import digest
from agent_backend.workspaces import workspace_volume_subpath


def environment(config: BucketConfig, password: str) -> dict[str, str]:
    home = config.state_root / "opencode-home"
    return {
        "HOME": home.as_posix(), "XDG_CONFIG_HOME": (home / "config").as_posix(),
        "XDG_DATA_HOME": (home / "data").as_posix(), "XDG_CACHE_HOME": (home / "cache").as_posix(),
        "XDG_STATE_HOME": (home / "state").as_posix(), "OPENCODE_PASSWORD": password,
        "OPENCODE_CONFIG": config.opencode.config_file.as_posix(),
        "OPENCODE_CONFIG_PROJECT_DISABLE": "true",
        "OPENCODE_CONFIG_CONTENT": json.dumps({"plugins": ["file://" + config.opencode.plugin_dir.as_posix()]}),
        "MANAGED_BUCKET": config.model_dump_json(),
    }


class BucketMissingError(RuntimeError):
    pass


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

    def deployment(self, bucket_id: str, frontend_id: str, password: str) -> dict:
        if frontend_id not in self.config.frontends:
            raise ValueError("bucket requires a registered frontend_id")
        frontend_id = frontend_segment(frontend_id)
        pool, runtime = self.pool, self.config.deployment_for(frontend_id, bucket_id)
        workspace_subpath = workspace_volume_subpath(runtime.workspace_root, frontend_id)
        labels = {"app": "opencode-bucket", "bucket-id": bucket_id}
        pod_labels = {**labels, "frontend-key": digest(frontend_id)[:16]}
        annotations = {"opencode.io/frontend-id": frontend_id}
        container = {
            "name": "opencode", "workingDir": runtime.opencode.context_root.as_posix(), "image": pool.image, "imagePullPolicy": pool.image_pull_policy,
            "command": [runtime.opencode.binary.as_posix()],
            "args": ["serve", "--hostname", "0.0.0.0", "--port", str(runtime.opencode.port)],
            "env": [{"name": key, "value": value} for key, value in environment(runtime, password).items()],
            "ports": [{"containerPort": runtime.opencode.port, "name": "opencode"}],
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
            "startupProbe": {"httpGet": self.probe(runtime, password),
                             "failureThreshold": max(30, pool.ready_timeout_seconds // 5), "periodSeconds": 5},
            "readinessProbe": {"httpGet": self.probe(runtime, password)},
            "livenessProbe": {"httpGet": self.probe(runtime, password)},
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

    @staticmethod
    def probe(config, password):
        token = base64.b64encode(("opencode:" + password).encode()).decode()
        return {"path": "/api/info", "port": config.opencode.port,
                "httpHeaders": [{"name": "Authorization", "value": "Basic " + token}]}

    async def endpoint(self, bucket_id: str, frontend_id: str, password: str) -> dict | None:
        response = await self.request("GET", f"/api/v1/namespaces/{self.pool.namespace}/pods",
            params={"labelSelector": f"app=opencode-bucket,bucket-id={bucket_id}"})
        response.raise_for_status()
        pods = [p for p in response.json()["items"] if not p["metadata"].get("deletionTimestamp")
                and p.get("status", {}).get("phase") == "Running" and p["status"].get("podIP")]
        if len(pods) != 1:
            return None
        pod = pods[0]
        if pod["metadata"].get("annotations", {}).get("opencode.io/frontend-id") != frontend_id:
            raise PermissionError("pod frontend mismatch")
        ip = pod["status"]["podIP"]
        if ":" in ip:
            ip = "[" + ip + "]"
        port = self.config.bucket_for(frontend_id).opencode.port
        return {"url": f"http://{ip}:{port}", "password": password,
                "generation": pod["metadata"]["uid"]}

    async def exists(self, bucket_id: str, frontend_id: str) -> bool:
        path = f"/apis/apps/v1/namespaces/{self.pool.namespace}/deployments/{bucket_id}"
        response = await self.request("GET", path)
        if response.status_code == 404:
            return False
        response.raise_for_status()
        metadata = response.json()["metadata"]
        if (metadata.get("labels", {}).get("bucket-id") != bucket_id
                or metadata.get("annotations", {}).get("opencode.io/frontend-id") != frontend_id):
            raise RuntimeError("Deployment ownership mismatch")
        return not metadata.get("deletionTimestamp")

    async def wait_stopped(self, bucket_id: str) -> None:
        async with asyncio.timeout(self.pool.ready_timeout_seconds):
            while True:
                response = await self.request("GET", f"/api/v1/namespaces/{self.pool.namespace}/pods",
                    params={"labelSelector": f"app=opencode-bucket,bucket-id={bucket_id}"})
                response.raise_for_status()
                if not response.json().get("items"):
                    return
                await asyncio.sleep(0.5)

    async def ensure(self, bucket_id: str, frontend_id: str, password: str, *, allow_create: bool = True) -> None:
        collection = f"/apis/apps/v1/namespaces/{self.pool.namespace}/deployments"
        for attempt in range(self._WRITE_ATTEMPTS):
            response = await self.request("GET", collection + "/" + bucket_id)
            if response.status_code == 404:
                if not allow_create:
                    raise BucketMissingError(bucket_id)
                response = await self.request("POST", collection, json=self.deployment(bucket_id, frontend_id, password))
                if response.status_code == 409:
                    response = await self.request("GET", collection + "/" + bucket_id)
            response.raise_for_status()
            current = response.json()
            if current.get("metadata", {}).get("deletionTimestamp"):
                raise BucketMissingError(bucket_id)
            if current.get("metadata", {}).get("labels", {}).get("bucket-id") != bucket_id:
                raise RuntimeError("existing Deployment is not owned by this bucket")
            owner = current.get("metadata", {}).get("annotations", {}).get("opencode.io/frontend-id")
            if owner != frontend_id:
                raise RuntimeError("existing Deployment belongs to another frontend")
            desired = self.deployment(bucket_id, frontend_id, password)
            desired["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
            response = await self.request("PUT", collection + "/" + bucket_id, json=desired)
            if not await self._retry_conflict(response, attempt):
                break
        try:
            await self.scale(bucket_id, 1)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise BucketMissingError(bucket_id) from exc
            raise

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
