"""Capacity-based placement with bindings only while a user has outstanding work."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from uuid import uuid4

from starlette.websockets import WebSocket

from agent_backend.config import BUCKET_SUFFIX_LENGTH, ControlConfig, bucket_name_prefix
from agent_backend.journal import Journal
from agent_backend.models import Identity
from agent_backend.wire import Peer
from agent_backend.workspaces import workspace_path
from container_up.bucket_manager import BucketManager, BucketMissing

log = logging.getLogger(__name__)


class BucketScheduler:
    def __init__(self, config: ControlConfig, db: Journal, interrupt_requests: Callable[[str], None]):
        self.config = config
        self.db = db
        self.peers: dict[str, Peer] = {}
        self.sockets: dict[str, WebSocket] = {}
        self.interrupt_requests = interrupt_requests
        self.manager = BucketManager(config)
        self.start_locks: dict[str, asyncio.Lock] = {}
        self.pins: dict[str, int] = {}
        self.reservations: dict[str, int] = {}
        self.stopped: dict[str, asyncio.Event] = {}
        self.connection_events: dict[str, asyncio.Event] = {}

    def connection_event(self, bucket_id: str) -> asyncio.Event:
        return self.connection_events.setdefault(bucket_id, asyncio.Event())

    def connected(self, bucket_id: str) -> bool:
        peer = self.peers.get(bucket_id)
        return bool(peer and not peer.closed)

    def _detach(self, bucket_id: str) -> WebSocket | None:
        self.peers.pop(bucket_id, None)
        self.connection_event(bucket_id).clear()
        return self.sockets.pop(bucket_id, None)

    async def disconnect(self, bucket_id: str) -> None:
        if socket := self._detach(bucket_id):
            await socket.close(code=1012)

    @asynccontextmanager
    async def connection(self, bucket_id: str, peer: Peer, socket: WebSocket):
        old = self.sockets.get(bucket_id)
        self.peers[bucket_id], self.sockets[bucket_id] = peer, socket
        self.connection_event(bucket_id).set()
        try:
            if old:
                await old.close(code=1012)
            yield
        finally:
            if self.peers.get(bucket_id) is peer:
                self._detach(bucket_id)

    def active_bucket(self, bucket_id: str) -> dict | None:
        bucket = self.db.get("bucket", bucket_id)
        return bucket if bucket and bucket["status"] in {"creating", "running", "draining"} else None

    def set_accepting(self, bucket_id: str, accepting: bool) -> None:
        bucket = self.db.get("bucket", bucket_id)
        if not bucket or not isinstance(accepting, bool):
            raise ValueError("invalid bucket status")
        self.db.put("bucket", bucket_id, {**bucket, "accepting": accepting})

    def bindings(self, bucket_id: str) -> list[dict]:
        return [b for b in self.db.records("binding") if b.get("bucket_id") == bucket_id]

    def busy(self, bucket_id: str) -> bool:
        return bool(self.pins.get(bucket_id) or any(
            bucket_id in (m["source"], m["target"]) for m in self.db.records("migration")) or any(
            r["bucket_id"] == bucket_id and not r.get("result") for r in self.db.records("request")))

    @contextmanager
    def hold(self, bucket_id: str):
        """Prevent a drain until the caller releases the bucket."""
        self.pins[bucket_id] = self.pins.get(bucket_id, 0) + 1
        try:
            yield
        finally:
            self.pins[bucket_id] -= 1

    def touch(self, bucket_id: str):
        bucket = self.db.get("bucket", bucket_id)
        if bucket:
            self.db.put("bucket", bucket_id, {**bucket, "last_active_at": time.time()})

    def release(self, identity_id: str) -> None:
        if self.reservations.get(identity_id):
            return
        if any(m["identity_id"] == identity_id for m in self.db.records("migration")):
            return
        if any(not row.get("result") and
               Identity.model_validate(row["submit"]["identity"]).identity_id == identity_id
               for row in self.db.records("request")):
            return
        self.db.delete("binding", identity_id)

    def reserve(self, identity: Identity, preferred_bucket: str | None = None) -> dict:
        """Commit the binding and capacity reservation together on the CU loop."""
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
            runtime = self.config.runtime_for(identity.frontend_id)
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
                          and len(self.bindings(b["bucket_id"])) < self.config.bucket_pool.max_users_per_bucket]
            movement = next((m for m in self.db.records("migration")
                             if m["identity_id"] == identity.identity_id), None)
            preferred_bucket = movement["target"] if movement else preferred_bucket
            if preferred_bucket:
                bucket = self.db.get("bucket", preferred_bucket)
                if not bucket or bucket["frontend_id"] != identity.frontend_id:
                    raise PermissionError("session bucket frontend mismatch")
            elif candidates:
                bucket = min(candidates, key=lambda b: (not self.connected(b["bucket_id"]),
                                                        len(self.bindings(b["bucket_id"]))))
            else:
                prefix = bucket_name_prefix(self.config.bucket_pool.name_prefix, identity.frontend_id)
                bucket_id = f"{prefix}-{uuid4().hex[:BUCKET_SUFFIX_LENGTH]}"
                bucket = {"bucket_id": bucket_id, "created_at": time.time(), "status": "creating"}
            bucket_id = bucket["bucket_id"]
            self.db.put("bucket", bucket_id, {**bucket, "frontend_id": identity.frontend_id,
                                              "last_active_at": time.time()})
            binding = {"bucket_id": bucket_id, "identity_id": identity.identity_id, "identity": identity.model_dump(),
                       "workspace_path": workspace}
            self.db.put("binding", identity.identity_id, binding)
            return binding

    async def ready(self, bucket_id: str, *, recover: bool = False):
        async with self.start_locks.setdefault(bucket_id, asyncio.Lock()), asyncio.timeout(self.config.bucket_pool.ready_timeout_seconds):
            bucket = self.db.get("bucket", bucket_id)
            if bucket["status"] == "missing" or (bucket["status"] != "creating" and
                    not await self.manager.exists(bucket_id, bucket["frontend_id"])):
                await self.missing(bucket_id)
                if not recover:
                    raise BucketMissing(bucket_id)
                bucket = {**self.db.get("bucket", bucket_id), "status": "creating"}
                self.db.put("bucket", bucket_id, bucket)
            if bucket["status"] == "draining":
                await self.stopped.setdefault(bucket_id, asyncio.Event()).wait()
            if not self.connected(bucket_id):
                bucket = self.db.get("bucket", bucket_id)
                connected = self.connection_event(bucket_id)
                connected.clear()
                try:
                    await self.manager.ensure(bucket_id, frontend_id=bucket["frontend_id"],
                                              allow_create=bucket["status"] == "creating")
                except BucketMissing:
                    await self.missing(bucket_id)
                    raise
                if not self.connected(bucket_id):
                    await connected.wait()
            if not await self.manager.exists(bucket_id, bucket["frontend_id"]):
                await self.missing(bucket_id)
                raise BucketMissing(bucket_id)
            bucket = self.db.get("bucket", bucket_id)
            self.db.put("bucket", bucket_id, {**bucket, "status": "running"})

    async def call_session(self, bucket_id: str, payload: dict) -> None:
        """Wake the original bucket on its persistent state to use native APIs."""
        bucket = self.db.get("bucket", bucket_id)
        if bucket["status"] == "draining":
            await self.stopped.setdefault(bucket_id, asyncio.Event()).wait()
            bucket = self.db.get("bucket", bucket_id)
        try:
            await self.ready(bucket_id, recover=True)
        except BucketMissing as exc:
            raise RuntimeError("native session bucket could not be restarted") from exc
        await self.peers[bucket_id].session_call(payload)

    async def missing(self, bucket_id: str):
        bucket = self.db.get("bucket", bucket_id)
        self.db.put("bucket", bucket_id, {**bucket, "status": "missing", "accepting": False})
        await self.disconnect(bucket_id)
        await self.manager.wait_stopped(bucket_id)
        self.interrupt_requests(bucket_id)
        self.stopped.setdefault(bucket_id, asyncio.Event()).set()

    @asynccontextmanager
    async def assignment(self, identity: Identity, preferred_bucket: str | None = None):
        identity_id = identity.identity_id
        while True:
            binding = self.reserve(identity, preferred_bucket)
            bucket_id = binding["bucket_id"]
            self.reservations[identity_id] = self.reservations.get(identity_id, 0) + 1
            with self.hold(bucket_id):
                try:
                    try:
                        if any(m["target"] == bucket_id for m in self.db.records("migration")):
                            await self.ready(bucket_id, recover=True)
                        else:
                            await self.ready(bucket_id)
                    except BucketMissing:
                        if any(m["target"] == bucket_id for m in self.db.records("migration")):
                            raise RuntimeError("migration target could not be restarted") from None
                        continue  # Retry startup. A loss after yield is handled by the submitter.
                    yield binding
                    return
                finally:
                    self.reservations[identity_id] -= 1
                    self.release(identity_id)
                    self.touch(bucket_id)

    async def sweep(self):
        for entry in self.db.records("bucket"):
            bucket_id = entry["bucket_id"]
            # Re-read after any await for the previous bucket. Check and mark
            # draining before yielding, so placement sees the new status.
            bucket = self.db.get("bucket", bucket_id)
            if bucket["status"] == "missing":
                await self.missing(bucket_id)
                continue
            if bucket["status"] == "idle":
                continue
            if bucket["status"] != "draining":
                ttl = self.config.frontend_idle_ttl_seconds[bucket["frontend_id"]]
                if ttl == 0 or time.time() - bucket["last_active_at"] < ttl:
                    continue
                if self.busy(bucket_id) or self.db.pending(bucket_id):
                    continue
                self.stopped.setdefault(bucket_id, asyncio.Event()).clear()
                self.db.put("bucket", bucket_id, {**bucket, "status": "draining"})
            # Draining is durable and excludes allocations while Kubernetes shuts down.
            # Other buckets must remain schedulable during a slow scale operation.
            peer = self.peers.get(bucket_id)
            if not await self.manager.exists(bucket_id, bucket["frontend_id"]):
                await self.missing(bucket_id)
                continue
            if not peer or peer.closed:
                await self.manager.ensure(bucket_id, frontend_id=bucket["frontend_id"], allow_create=False)
                continue
            try:
                await peer.session_call({"operation": "drain", "session_id": bucket_id})
            except (RuntimeError, ConnectionError):
                continue
            await self.manager.scale(bucket_id, 0)
            await self.disconnect(bucket_id)
            bucket = self.db.get("bucket", bucket_id)
            if bucket["status"] == "draining":
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
            await asyncio.sleep(self.config.bucket_pool.sweep_interval_seconds)
