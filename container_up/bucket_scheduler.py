"""Capacity-based placement with bindings only while a user has outstanding work."""
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
        self.reservations: dict[str, int] = {}
        self.task = None
        self.stopped: dict[str, asyncio.Event] = {}

    def connected(self, bucket_id: str) -> bool:
        peer = self.service.peers.get(bucket_id)
        return bool(peer and not peer.closed)

    def bindings(self, bucket_id: str) -> list[dict]:
        return [b for b in self.db.records("binding") if b.get("bucket_id") == bucket_id]

    def busy(self, bucket_id: str) -> bool:
        return bool(self.pins.get(bucket_id) or any(
            r["bucket_id"] == bucket_id and not r.get("result") for r in self.db.records("request")))

    def touch(self, bucket_id: str):
        bucket = self.db.get("bucket", bucket_id)
        if bucket:
            self.db.put("bucket", bucket_id, {**bucket, "last_active_at": time.time()})

    def release(self, identity_id: str) -> None:
        if self.reservations.get(identity_id):
            return
        if any(not row.get("result") and
               Identity.model_validate(row["submit"]["identity"]).identity_id == identity_id
               for row in self.db.records("request")):
            return
        self.db.delete("binding", identity_id)

    def reserve(self, identity: Identity) -> dict:
        """Called under lock; mapping and capacity reservation commit together."""
        with self.db.transaction():
            for binding in self.db.records("binding"):
                self.release(binding["identity_id"])
            existing = self.db.get("binding", identity.identity_id)
            if existing:
                bucket = self.db.get("bucket", existing["bucket_id"])
                if not bucket or bucket["frontend_id"] != identity.frontend_id:
                    raise ValueError("bucket frontend ownership mismatch")
                self.touch(bucket["bucket_id"])
                return existing
            runtime = self.service.config.runtime_for(identity.frontend_id)
            workspace = str(workspace_path(runtime.workspace_root, identity))
            # The original sanitizer can collide. Never grant two identities the same directory.
            owner = self.db.get("workspace_owner", workspace)
            if owner and owner["identity_id"] != identity.identity_id:
                raise ValueError("workspace path collides with another user")
            self.db.put("workspace_owner", workspace, {"identity_id": identity.identity_id})
            candidates = [b for b in self.db.records("bucket") if b["status"] in {"creating", "running"}
                          and (b["status"] == "creating" or self.connected(b["bucket_id"]))
                          and b.get("accepting", True)
                          and b["frontend_id"] == identity.frontend_id
                          and len(self.bindings(b["bucket_id"])) < self.config.max_users_per_bucket]
            if candidates:
                bucket = min(candidates, key=lambda b: (not self.connected(b["bucket_id"]),
                                                        len(self.bindings(b["bucket_id"]))))
            else:
                bucket_id = self.config.name_prefix + "-" + uuid4().hex[:12]
                bucket = {"bucket_id": bucket_id, "created_at": time.time(), "status": "creating"}
            bucket_id = bucket["bucket_id"]
            self.db.put("bucket", bucket_id, {**bucket, "frontend_id": identity.frontend_id,
                                              "last_active_at": time.time()})
            binding = {"bucket_id": bucket_id, "identity_id": identity.identity_id, "identity": identity.model_dump(),
                       "workspace_path": workspace}
            self.db.put("binding", identity.identity_id, binding)
            return binding

    async def ready(self, bucket_id: str):
        async with self.start_locks.setdefault(bucket_id, asyncio.Lock()), asyncio.timeout(self.config.ready_timeout_seconds):
            bucket = self.db.get("bucket", bucket_id)
            if bucket["status"] == "draining":
                await self.stopped.setdefault(bucket_id, asyncio.Event()).wait()
            if not self.connected(bucket_id):
                bucket = self.db.get("bucket", bucket_id)
                self.db.put("bucket", bucket_id, {**bucket, "status": "creating"})
                connected = self.service.bucket_connected.setdefault(bucket_id, asyncio.Event())
                connected.clear()
                await self.manager.ensure(bucket_id, frontend_id=bucket["frontend_id"])
                if not self.connected(bucket_id):
                    await connected.wait()
            bucket = self.db.get("bucket", bucket_id)
            self.db.put("bucket", bucket_id, {**bucket, "status": "running"})

    @asynccontextmanager
    async def assignment(self, identity: Identity):
        async with self.lock:
            binding = self.reserve(identity)
            bucket_id = binding["bucket_id"]
            self.pins[bucket_id] = self.pins.get(bucket_id, 0) + 1
            self.reservations[identity.identity_id] = self.reservations.get(identity.identity_id, 0) + 1
        try:
            await self.ready(bucket_id)
            yield binding
        finally:
            self.pins[bucket_id] -= 1
            self.reservations[identity.identity_id] -= 1
            self.release(identity.identity_id)
            self.touch(bucket_id)

    async def sweep(self):
        for bucket in self.db.records("bucket"):
            bucket_id = bucket["bucket_id"]
            async with self.lock:
                bucket = self.db.get("bucket", bucket_id)
                if bucket["status"] == "idle" or (bucket["status"] != "draining" and self.busy(bucket_id)):
                    continue
                if bucket["status"] != "draining":
                    ttl = self.service.config.frontend_idle_ttl_seconds[bucket["frontend_id"]]
                    if ttl == 0 or time.time() - bucket["last_active_at"] < ttl:
                        continue
                    if self.db.pending(bucket_id):
                        continue  # Never discard an unacknowledged command.
                    self.stopped.setdefault(bucket_id, asyncio.Event()).clear()
                    self.db.put("bucket", bucket_id, {**bucket, "status": "draining"})
            # Draining is durable and excludes allocations while Kubernetes shuts down.
            # Other buckets must remain schedulable during a slow scale operation.
            peer = self.service.peers.get(bucket_id)
            if not peer or peer.closed:
                await self.manager.ensure(bucket_id, frontend_id=bucket["frontend_id"])
                continue
            try:
                await peer.session_call({"operation": "drain", "session_id": bucket_id})
            except (RuntimeError, ConnectionError):
                continue
            await self.manager.scale(bucket_id, 0)
            socket = self.service.sockets.get(bucket_id)
            if socket:
                await socket.close(code=1012)
            async with self.lock:
                self.service.peers.pop(bucket_id, None)
                self.service.sockets.pop(bucket_id, None)
                bucket = self.db.get("bucket", bucket_id)
                self.db.put("bucket", bucket_id, {**bucket, "status": "idle"})
                self.stopped.setdefault(bucket_id, asyncio.Event()).set()

    async def run(self):
        while True:
            try:
                # Finish an interrupted shutdown before waking the same bucket.
                await self.sweep()
                # Recover accepted work after a CU restart or missing workload. Do not reassign it.
                for bucket in self.db.records("bucket"):
                    if bucket["status"] in {"creating", "running"} and self.busy(bucket["bucket_id"]):
                        await self.ready(bucket["bucket_id"])
            except Exception:
                log.exception("bucket pool reconciliation failed; will retry")
            await asyncio.sleep(self.config.sweep_interval_seconds)

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
