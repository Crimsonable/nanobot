import asyncio
import copy
import time

import httpx

from agent_backend.config import BucketSettings


class FakeNative:
    def __init__(self):
        self.sessions, self.history, self.inputs = {}, {}, {}
        self.deleted, self.submissions = [], []
        self.active = False
        self.jobs = []
        self.http = httpx.AsyncClient(
            base_url="http://native", transport=httpx.MockTransport(self.respond)
        )

    def respond(self, request):
        sid = request.url.path.split("/")[3]
        if request.url.path.endswith("/message"):
            return httpx.Response(
                200, json={"data": list(reversed(self.history.get(sid, []))), "cursor": {}}
            )
        return httpx.Response(404)

    async def session(self, sid):
        return self.sessions[sid]

    async def ready(self):
        return True

    async def close(self):
        await self.http.aclose()

    async def create_session(self, sid, directory, owner):
        if sid not in self.sessions:
            self.sessions[sid] = {
                "id": sid,
                "location": {"directory": str(directory)},
                "metadata": {"managed": owner},
                "title": "Managed conversation",
                "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
            }
            self.history[sid] = []
        assert self.sessions[sid]["metadata"]["managed"] == owner
        return self.sessions[sid]

    async def submit(self, native, text, metadata):
        self.submissions.append(native["message_id"])
        if native["message_id"] in self.inputs:
            return
        self.inputs[native["message_id"]] = metadata
        self.history[native["session_id"]].append(
            {
                "id": native["message_id"],
                "type": "user",
                "text": text,
                "metadata": {"managed": metadata},
                "time": {"created": 1000},
            }
        )

    async def tree(self, sid, **kwargs):
        if sid not in self.sessions and kwargs.get("missing_ok"):
            return []
        result = [self.sessions[sid]]
        for info in result:
            result.extend(s for s in self.sessions.values() if s.get("parentID") == info["id"])
        return copy.deepcopy(result)

    async def idle(self, tree):
        return not self.active and not self.jobs

    async def cancel_input(self, native):
        self.active = False

    async def call(self, method, path, **kwargs):
        return {
            "data": {"s": {}}
            if self.active and path.endswith("/active")
            else self.jobs
            if path.endswith("/job")
            else []
        }

    async def export_session(self, sid):
        return copy.deepcopy({"info": self.sessions[sid], "messages": self.history[sid]})

    async def import_session(self, data, directory):
        sid = data["info"]["id"]
        if sid not in self.sessions:
            self.sessions[sid], self.history[sid] = (
                copy.deepcopy(data["info"]),
                copy.deepcopy(data["messages"]),
            )

    async def delete_session(self, sid):
        self.deleted.append(sid)
        self.sessions.pop(sid, None)
        self.history.pop(sid, None)

    def answer(self, row, text="done", *, idle=True, error=None):
        sid, mid = row["native"]["session_id"], row["native"]["message_id"]
        self.history[sid].append(
            {
                "id": mid + "answer",
                "type": "assistant",
                "agent": "build",
                "model": {"providerID": "test", "id": "test"},
                "content": [{"type": "text", "text": text}],
                "finish": "stop",
                "time": {"created": 1100, "completed": 1200},
                "tokens": {
                    "input": 12,
                    "output": 4,
                    "reasoning": 0,
                    "cache": {"read": 0, "write": 0},
                },
            }
        )
        if idle:
            self.history[sid].append(
                {
                    "id": mid + "idle",
                    "type": "idle",
                    "outcome": "failed" if error else "succeeded",
                    "time": {"created": 1300},
                    **({"error": error} if error else {}),
                }
            )


class FakeManager:
    def __init__(self, service):
        self.service, self.created, self.scaled = service, [], []
        self.missing = set()
        self.fail_start = self.fail_stop = False
        self.natives = {}

    async def exists(self, bucket_id, frontend_id):
        return bucket_id not in self.missing

    async def wait_stopped(self, bucket_id):
        pass

    async def ensure(self, bucket_id, frontend_id, password, *, allow_create=True):
        self.created.append(bucket_id)
        await asyncio.sleep(0)
        if self.fail_start:
            raise RuntimeError("Kubernetes unavailable")
        self.missing.discard(bucket_id)

    async def endpoint(self, bucket_id, frontend_id, password):
        return {"url": "http://" + bucket_id, "password": password}

    async def scale(self, bucket_id, replicas):
        if self.fail_stop:
            raise RuntimeError("scale failed")
        self.scaled.append((bucket_id, replicas))

    async def client(self, bucket_id):
        return (
            self.natives.setdefault(bucket_id, FakeNative())
            if bucket_id not in self.natives
            else self.natives[bucket_id]
        )


def configure_pool(service):
    manager = FakeManager(service)
    service.pool.manager = manager
    service.pool.client = manager.client
    return manager


def register_bucket(service, bucket_id="b", frontend_id="web"):
    if not isinstance(service.pool.manager, FakeManager):
        configure_pool(service)
    service.config.bucket_templates[frontend_id] = BucketSettings(
        workspace_root=service.config.state_root / "users"
    )
    service.config.frontend_idle_ttl_seconds[frontend_id] = 0
    service.db.put(
        "bucket",
        bucket_id,
        {
            "bucket_id": bucket_id,
            "frontend_id": frontend_id,
            "behavior": "pool",
            "owner": None,
            "status": "running",
            "opencode": {"url": "http://" + bucket_id, "password": "test"},
            "password": "test",
            "last_active_at": time.time(),
        },
    )


class LocalControl:
    def __init__(self, config):
        from container_up.server import ControlService

        self.service = ControlService(config)
        register_bucket(self.service)

    async def submit(self, frontend, body):
        from agent_backend.ingress import Inbound

        return await self.service.submit(frontend, Inbound.model_validate(body))

    async def request(self, frontend, request_id):
        return self.service.db.get("request", request_id)

    async def bucket(self, bucket_id):
        return self.service.db.get("bucket", bucket_id)

    async def cancel(self, frontend, request_id):
        await self.service.cancel(request_id)

    async def collected(self, frontend, request_id, result):
        from agent_backend.models import ExecutionStatus, Submit

        self.service.complete(ExecutionStatus.model_validate(result))
        row = self.service.db.get("request", request_id)
        self.service.db.put("request", request_id, {**row, "output_collected": True})
        self.service.pool.release(Submit.model_validate(row["submit"]).session_key)

    async def close(self):
        for native in self.service.pool.manager.natives.values():
            await native.close()
        self.service.db.close()
