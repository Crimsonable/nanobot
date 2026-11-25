"""Route sessions to a shared pool or a deterministic user-owned bucket."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from uuid import uuid4

import httpx

from agent_backend.config import BUCKET_SUFFIX_LENGTH, ControlConfig, bucket_name_prefix, individual_bucket_id
from agent_backend.journal import Journal
from agent_backend.models import Identity, Submit, session_key
from agent_backend.opencode import OpenCode
from agent_backend.session_history import SessionBusyError
from agent_backend.workspaces import workspace_path
from container_up.bucket_manager import BucketManager, BucketMissingError

log = logging.getLogger(__name__)


class BucketScheduler:
    def __init__(self, config: ControlConfig, db: Journal, interrupt_requests: Callable[[str], None]):
        self.config = config
        self.db = db
        self.clients: dict[str, tuple[dict, OpenCode]] = {}
        self.interrupt_requests = interrupt_requests
        self.manager = BucketManager(config, db)
        self.start_locks: dict[str, asyncio.Lock] = {}
        self.pins: dict[str, int] = {}
        self.reservations: dict[str, int] = {}
        self.stopped: dict[str, asyncio.Event] = {}

    def check_owner(self, bucket: dict, identity: Identity) -> None:
        if bucket["frontend_id"] != identity.frontend_id:
            raise PermissionError("session bucket frontend mismatch")
        if bucket["behavior"] == "individual" and (
                bucket["owner"] != identity.model_dump()
                or bucket["bucket_id"] != individual_bucket_id(identity)):
            raise PermissionError("individual bucket owner mismatch")

    def matches_mode(self, bucket: dict, identity: Identity) -> bool:
        self.check_owner(bucket, identity)
        return bucket["behavior"] == self.config.bucket_for(identity.frontend_id).behavior

    def connected(self, bucket_id: str) -> bool:
        bucket = self.db.get("bucket", bucket_id)
        return bool(bucket and bucket.get("opencode") and bucket["status"] == "running")

    async def client(self, bucket_id: str) -> OpenCode:
        endpoint = self.db.get("bucket", bucket_id).get("opencode")
        if not endpoint:
            await self.ready(bucket_id, recover=True)
            endpoint = self.db.get("bucket", bucket_id)["opencode"]
        current = self.clients.get(bucket_id)
        if not current or current[0] != endpoint:
            if current:
                await current[1].close()
            current = (endpoint, OpenCode(endpoint))
            self.clients[bucket_id] = current
        return current[1]

    async def close(self):
        for _, client in self.clients.values():
            await client.close()

    def active_bucket(self, bucket_id: str) -> dict | None:
        bucket = self.db.get("bucket", bucket_id)
        return bucket if bucket and bucket["status"] in {"creating", "running", "draining"} else None

    def bindings(self, bucket_id: str) -> list[dict]:
        return [b for b in self.db.records("binding") if b.get("bucket_id") == bucket_id]

    def busy(self, bucket_id: str) -> bool:
        return bool(self.pins.get(bucket_id) or any(
            bucket_id in (m["source"], m["target"]) for m in self.db.records("migration")) or any(
            r["bucket_id"] == bucket_id and not r.get("output_collected") for r in self.db.records("request")))

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

    def release(self, key: str) -> None:
        if self.reservations.get(key):
            return
        if self.db.get("migration", key):
            return
        if any(not row.get("output_collected") and
               Submit.model_validate(row["submit"]).session_key == key
               for row in self.db.records("request")):
            return
        self.db.delete("binding", key)

    def reserve(self, identity: Identity, chat_id: str, preferred_bucket: str | None = None) -> dict:
        """Commit the binding and capacity reservation together on the CU loop."""
        key = session_key(identity, chat_id)
        with self.db.transaction():
            for binding in self.db.records("binding"):
                self.release(binding["session_key"])
            movement = self.db.get("migration", key)
            existing = self.db.get("binding", key)
            if existing:
                bucket = self.db.get("bucket", existing["bucket_id"])
                if not bucket or bucket["frontend_id"] != identity.frontend_id:
                    raise ValueError("bucket frontend ownership mismatch")
                if not self.matches_mode(bucket, identity) and not movement:
                    raise SessionBusyError(bucket["bucket_id"])
                self.touch(bucket["bucket_id"])
                return existing
            runtime = self.config.bucket_for(identity.frontend_id)
            workspace = str(workspace_path(runtime.workspace_root, identity))
            # The original sanitizer can collide. Never grant two identities the same directory.
            owner = self.db.get("workspace_owner", workspace)
            if owner and owner["identity_id"] != identity.identity_id:
                raise ValueError("workspace path collides with another user")
            self.db.put("workspace_owner", workspace, {"identity_id": identity.identity_id})
            candidates = []
            if runtime.behavior == "pool":
                candidates = [b for b in self.db.records("bucket") if b["behavior"] == "pool"
                              and b["status"] in {"creating", "running"}
                              and (b["status"] == "creating" or self.connected(b["bucket_id"]))
                              and b["frontend_id"] == identity.frontend_id
                              and len(self.bindings(b["bucket_id"])) < self.config.bucket_pool.max_sessions_per_bucket]
            preferred_bucket = movement["target"] if movement else preferred_bucket
            if runtime.behavior == "individual" and not movement:
                preferred_bucket = individual_bucket_id(identity)
                if not self.db.get("bucket", preferred_bucket):
                    self.db.put("bucket", preferred_bucket, self.new_bucket(preferred_bucket, identity, runtime.behavior))
            if not preferred_bucket and (catalog := self.db.get("session", key)):
                previous = self.db.get("bucket", catalog["bucket_id"])
                # History affinity precedes load balancing. A dormant bucket can
                # wake in place; an interrupted transfer must keep its recorded target.
                if (previous and previous["frontend_id"] == identity.frontend_id
                        and previous["behavior"] == "pool"
                        and previous["status"] in {"creating", "running", "idle", "draining"}
                        and len(self.bindings(previous["bucket_id"])) < self.config.bucket_pool.max_sessions_per_bucket):
                    preferred_bucket = previous["bucket_id"]
            if preferred_bucket:
                bucket = self.db.get("bucket", preferred_bucket)
                if not bucket or bucket["frontend_id"] != identity.frontend_id:
                    raise PermissionError("session bucket frontend mismatch")
                self.check_owner(bucket, identity)
                if not movement and not self.matches_mode(bucket, identity):
                    raise PermissionError("bucket does not match frontend routing mode")
            elif candidates:
                bucket = min(candidates, key=lambda b: (not self.connected(b["bucket_id"]),
                                                        len(self.bindings(b["bucket_id"]))))
            else:
                prefix = bucket_name_prefix(self.config.bucket_pool.name_prefix, identity.frontend_id)
                bucket_id = f"{prefix}-{uuid4().hex[:BUCKET_SUFFIX_LENGTH]}"
                bucket = self.new_bucket(bucket_id, identity, runtime.behavior)
            bucket_id = bucket["bucket_id"]
            self.db.put("bucket", bucket_id, {**bucket, "frontend_id": identity.frontend_id,
                                              "last_active_at": time.time()})
            binding = {"bucket_id": bucket_id, "identity_id": identity.identity_id, "identity": identity.model_dump(),
                       "session_key": key, "chat_id": chat_id, "workspace_path": workspace}
            self.db.put("binding", key, binding)
            return binding

    @staticmethod
    def new_bucket(bucket_id, identity, behavior):
        return {"bucket_id": bucket_id, "frontend_id": identity.frontend_id,
                "behavior": behavior,
                "owner": identity.model_dump() if behavior == "individual" else None,
                "created_at": time.time(), "last_active_at": time.time(), "status": "creating",
                "password": secrets.token_urlsafe(32)}

    async def ready(self, bucket_id: str, *, recover: bool = False):
        async with self.start_locks.setdefault(bucket_id, asyncio.Lock()), asyncio.timeout(self.config.bucket_pool.ready_timeout_seconds):
            bucket = self.db.get("bucket", bucket_id)
            if bucket["status"] == "draining":
                await self.stopped.setdefault(bucket_id, asyncio.Event()).wait()
                bucket = self.db.get("bucket", bucket_id)
            if bucket["status"] == "missing" or (bucket["status"] != "creating" and
                    not await self.manager.exists(bucket_id, bucket["frontend_id"])):
                await self.missing(bucket_id)
                if not recover:
                    raise BucketMissingError(bucket_id)
                bucket = {**self.db.get("bucket", bucket_id), "status": "creating"}
                self.db.put("bucket", bucket_id, bucket)
            if bucket["status"] != "running" or not bucket.get("opencode"):
                await self.manager.ensure(bucket_id, bucket["frontend_id"], bucket["password"],
                                          allow_create=bucket["status"] == "creating")
            while True:
                endpoint = await self.manager.endpoint(bucket_id, bucket["frontend_id"], bucket["password"])
                if endpoint:
                    self.db.put("bucket", bucket_id, {**self.db.get("bucket", bucket_id), "opencode": endpoint})
                    try:
                        if await (await self.client(bucket_id)).ready():
                            break
                    except httpx.HTTPError:
                        pass
                await asyncio.sleep(0.5)
            self.db.put("bucket", bucket_id, {**self.db.get("bucket", bucket_id), "status": "running"})

    async def missing(self, bucket_id: str):
        bucket = self.db.get("bucket", bucket_id)
        self.db.put("bucket", bucket_id, {**bucket, "status": "missing", "opencode": None})
        await self.manager.wait_stopped(bucket_id)
        self.interrupt_requests(bucket_id)
        self.stopped.setdefault(bucket_id, asyncio.Event()).set()

    @asynccontextmanager
    async def assignment(self, identity: Identity, chat_id: str, preferred_bucket: str | None = None):
        key = session_key(identity, chat_id)
        while True:
            binding = self.reserve(identity, chat_id, preferred_bucket)
            bucket_id = binding["bucket_id"]
            self.reservations[key] = self.reservations.get(key, 0) + 1
            with self.hold(bucket_id):
                try:
                    workspace = workspace_path(self.config.bucket_for(identity.frontend_id).workspace_root, identity)
                    if workspace.resolve() != workspace.absolute():
                        raise PermissionError("workspace cannot contain symbolic links")
                    workspace.mkdir(parents=True, exist_ok=True)
                    try:
                        if (self.db.get("bucket", bucket_id)["behavior"] == "individual"
                                or any(m["target"] == bucket_id for m in self.db.records("migration"))):
                            await self.ready(bucket_id, recover=True)
                        else:
                            await self.ready(bucket_id)
                    except BucketMissingError:
                        if any(m["target"] == bucket_id for m in self.db.records("migration")):
                            raise RuntimeError("migration target could not be restarted") from None
                        continue  # Retry startup. A loss after yield is handled by the submitter.
                    yield binding
                    return
                finally:
                    self.reservations[key] -= 1
                    self.release(key)
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
                if self.busy(bucket_id):
                    continue
                self.stopped.setdefault(bucket_id, asyncio.Event()).clear()
                self.db.put("bucket", bucket_id, {**bucket, "status": "draining"})
            # Draining is durable and excludes allocations while Kubernetes shuts down.
            # Other buckets must remain schedulable during a slow scale operation.
            if not await self.manager.exists(bucket_id, bucket["frontend_id"]):
                await self.missing(bucket_id)
                continue
            client = await self.client(bucket_id)
            if (await client.call("GET", "/api/session/active"))["data"]:
                continue
            if (await client.call("GET", "/api/experimental/job"))["data"]:
                continue
            await self.manager.scale(bucket_id, 0)
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
