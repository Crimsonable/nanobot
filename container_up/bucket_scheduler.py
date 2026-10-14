"""Database-backed bucket pool, adapted from the original reservation/release mechanism.

Capacity counts distinct users, but only whole buckets have a lifecycle.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from agent_backend.models import Identity
from agent_backend.workspaces import workspace_path
from container_up.bucket_manager import BucketManager

log = logging.getLogger(__name__)


class BucketScheduler:
    def __init__(self, service, manager=None):
        self.service, self.db = service, service.db
        self.config = service.config.bucket_pool
        self.manager = manager or BucketManager(service.config)
        self.lock = asyncio.Lock()
        self.start_locks: dict[str, asyncio.Lock] = {}
        self.pins: dict[str, int] = {}
        self.task = None

    def connected(self, bucket_id: str) -> bool:
        peer = self.service.peers.get(bucket_id)
        return bool(peer and not peer.closed)

    def bindings(self, bucket_id: str) -> list[dict]:
        return [b for b in self.db.records("binding") if b.get("bucket_id") == bucket_id]

    def frontend_for(self, bucket: dict) -> str | None:
        if bucket.get("frontend_id"):
            return bucket["frontend_id"]
        # Old single-frontend buckets can be identified from their durable bindings.
        owners = {b["identity"]["frontend_id"] for b in self.bindings(bucket["bucket_id"])}
        if len(owners) == 1:
            return next(iter(owners))
        if not owners and len(self.service.config.frontends) == 1:
            return next(iter(self.service.config.frontends))
        return None

    def busy(self, bucket_id: str) -> bool:
        return bool(self.pins.get(bucket_id) or any(
            r["bucket_id"] == bucket_id and not r.get("result") for r in self.db.records("request")))

    def touch(self, bucket_id: str):
        bucket = self.db.get("bucket", bucket_id)
        if bucket:
            self.db.put("bucket", bucket_id, {**bucket, "last_active_at": time.time()})

    def reserve(self, identity: Identity) -> dict:
        """Called under lock; mapping and capacity reservation commit together."""
        with self.db.transaction():
            existing = self.db.get("binding", identity.identity_id)
            if existing and existing.get("bucket_id"):
                bucket = self.db.get("bucket", existing["bucket_id"])
                if bucket and bucket["status"] in {"creating", "running"}:
                    if self.frontend_for(bucket) != identity.frontend_id:
                        raise ValueError("bucket frontend ownership mismatch")
                    self.touch(bucket["bucket_id"])
                    return existing
                if self.busy(existing["bucket_id"]):
                    raise RuntimeError("previous bucket still has unfinished requests")
            runtime = self.service.config.runtime_for(identity.frontend_id)
            workspace = str(workspace_path(runtime.workspace_root, identity, runtime.workspace_layout))
            # The original sanitizer can collide. Never grant two identities the same directory.
            owner = self.db.get("workspace_owner", workspace)
            if owner and owner["identity_id"] != identity.identity_id:
                raise ValueError("workspace path collides with another user")
            self.db.put("workspace_owner", workspace, {"identity_id": identity.identity_id})
            candidates = [b for b in self.db.records("bucket") if b["status"] in {"creating", "running"}
                          and self.frontend_for(b) == identity.frontend_id
                          and len(self.bindings(b["bucket_id"])) < self.config.max_users_per_bucket]
            if candidates:
                bucket = min(candidates, key=lambda b: (not self.connected(b["bucket_id"]),
                                                        len(self.bindings(b["bucket_id"]))))
            else:
                # Reuse an empty, scaled-down workload before allocating another one.
                bucket = next((b for b in self.db.records("bucket") if b["status"] == "idle"
                               and self.frontend_for(b) == identity.frontend_id), None)
                if bucket is None:
                    bucket_id = self.config.name_prefix + "-" + uuid4().hex[:12]
                    bucket = {"bucket_id": bucket_id, "created_at": time.time()}
                bucket = {**bucket, "status": "creating"}
            bucket_id = bucket["bucket_id"]
            self.db.put("bucket", bucket_id, {**bucket, "frontend_id": identity.frontend_id,
                                              "last_active_at": time.time()})
            binding = {"bucket_id": bucket_id, "identity_id": identity.identity_id, "identity": identity.model_dump(),
                       "workspace_path": workspace, "generation": uuid4().hex, "status": "assigned"}
            self.db.put("binding", identity.identity_id, binding)
            return binding

    async def ready(self, bucket_id: str):
        async with self.start_locks.setdefault(bucket_id, asyncio.Lock()):
            if not self.connected(bucket_id):
                async with asyncio.timeout(self.config.ready_timeout_seconds):
                    bucket = self.db.get("bucket", bucket_id)
                    frontend_id = self.frontend_for(bucket)
                    if frontend_id is None:
                        raise RuntimeError("legacy bucket frontend is ambiguous; retire it before migration")
                    await self.manager.ensure(bucket_id, frontend_id=frontend_id)
                    while not self.connected(bucket_id):
                        await asyncio.sleep(0.1)
            bucket = self.db.get("bucket", bucket_id)
            self.db.put("bucket", bucket_id, {**bucket, "status": "running"})

    @asynccontextmanager
    async def assignment(self, identity: Identity):
        async with self.lock:
            binding = self.reserve(identity)
            bucket_id = binding["bucket_id"]
            self.pins[bucket_id] = self.pins.get(bucket_id, 0) + 1
        try:
            await self.ready(bucket_id)
            yield binding
        except BaseException:
            # Keep durable mappings for accepted work; release failed pre-dispatch reservations.
            async with self.lock:
                outstanding = any(r["submit"]["identity"] == identity.model_dump() and not r.get("result")
                                  for r in self.db.records("request"))
                current = self.db.get("binding", identity.identity_id)
                if not outstanding and current and current.get("generation") == binding["generation"]:
                    self.db.put("binding", identity.identity_id, {**current, "bucket_id": None, "status": "released"})
            raise
        finally:
            self.pins[bucket_id] -= 1
            self.touch(bucket_id)

    async def sweep(self):
        for bucket in self.db.records("bucket"):
            bucket_id = bucket["bucket_id"]
            async with self.lock:
                bucket = self.db.get("bucket", bucket_id)
                if bucket["status"] == "idle" or self.busy(bucket_id):
                    continue
                if bucket["status"] != "draining":
                    ttl = self.service.config.frontend_idle_ttl_seconds.get(
                        self.frontend_for(bucket), self.service.config.instance_idle_ttl_seconds)
                    if ttl == 0 or time.time() - bucket["last_active_at"] < ttl:
                        continue
                    if self.db.pending(bucket_id):
                        continue  # Never discard an unacknowledged command.
                    self.db.put("bucket", bucket_id, {**bucket, "status": "draining"})
            # Draining is durable and excludes allocations while Kubernetes shuts down.
            # Other buckets must remain schedulable during a slow scale operation.
            await self.manager.scale(bucket_id, 0)
            socket = self.service.sockets.get(bucket_id)
            if socket:
                await socket.close(code=1012)
            async with self.lock:
                self.service.peers.pop(bucket_id, None)
                self.service.sockets.pop(bucket_id, None)
                with self.db.transaction():
                    for binding in self.bindings(bucket_id):
                        self.db.put("binding", binding["identity_id"],
                                    {**binding, "bucket_id": None, "status": "released"})
                    self.db.put("bucket", bucket_id, {**bucket, "status": "idle"})

    async def run(self):
        while True:
            try:
                # Recover accepted work after a CU restart or missing workload. Do not reassign it.
                for bucket in self.db.records("bucket"):
                    if bucket["status"] in {"creating", "running"} and self.busy(bucket["bucket_id"]):
                        await self.ready(bucket["bucket_id"])
                await self.sweep()
            except Exception:
                log.exception("bucket pool reconciliation failed; will retry")
            await asyncio.sleep(self.config.sweep_interval_seconds)

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
