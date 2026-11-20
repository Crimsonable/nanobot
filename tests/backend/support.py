import time
from types import SimpleNamespace

from agent_backend.config import RuntimeSettings


def register_bucket(service, bucket_id="b", frontend_id="web"):
    from unittest.mock import AsyncMock
    service.pool.manager.exists = AsyncMock(return_value=True)
    service.pool.manager.wait_stopped = AsyncMock()
    service.config.runtime_templates[frontend_id] = RuntimeSettings(workspace_root=service.config.state_root / "users")
    service.config.frontend_idle_ttl_seconds[frontend_id] = 0
    service.db.put("bucket", bucket_id, {"bucket_id": bucket_id, "frontend_id": frontend_id,
        "status": "running", "created_at": time.time(), "last_active_at": time.time()})
    service.pool.peers[bucket_id] = SimpleNamespace(closed=False)


def native(runtime, session_id, kind, data=None, *, event_id=None, directory=None):
    from uuid import uuid4
    event_id = event_id or uuid4().hex
    row = next((r for r in runtime.db.records("run") if r.get("session_id") == session_id), {})
    directory = directory or row.get("directory", "global")
    event = {"id": event_id, "type": kind, "created": 1000,
             "data": {"sessionID": session_id, **(data or {})}}
    body = {"type": "opencode.event", "directory": directory, "event_id": event_id, "event": event}
    runtime.native_event(body)
    while runtime.process_next_event():
        pass
    return body


def emit_answer(runtime, session_id, input_id, directory, text="done", *, message_id="answer",
                finish="stop", tokens=None, complete=True):
    native(runtime, session_id, "session.inbox.enqueued", {"inboxID": input_id,
        "item": {"type": "user", "payload": {"text": "hello"}, "delivery": "queue"}}, directory=directory)
    native(runtime, session_id, "session.inbox.delivered", {"inboxID": input_id})
    native(runtime, session_id, "session.execution.started")
    base = {"assistantMessageID": message_id}
    native(runtime, session_id, "session.step.started", {**base, "started": 1000,
        "agent": "build", "model": {"providerID": "test", "id": "test"}})
    native(runtime, session_id, "session.text.started", {**base, "ordinal": 0})
    native(runtime, session_id, "session.text.delta", {**base, "ordinal": 0, "delta": text})
    native(runtime, session_id, "session.text.ended", {**base, "ordinal": 0, "text": text})
    last = native(runtime, session_id, "session.step.ended", {**base, "finish": finish,
        "cost": 0, "tokens": tokens or {"input": 12, "output": 4, "reasoning": 0, "cache": {"read": 0, "write": 0}}})
    if complete:
        last = native(runtime, session_id, "session.execution.succeeded")
    return last


def plugin_scope(runtime, session_id, call_id, tool, args):
    """Exercise TypeScript permissions with the scope returned by BU."""
    import json
    import os
    import subprocess
    from pathlib import Path

    from agent_backend.models import Submit
    row = next((r for r in runtime.db.records("run") if r.get("session_id") == session_id), None)
    if row is None:
        raise PermissionError("unknown test session")
    scope = runtime.execution_scope(Submit.model_validate(row["submit"]))
    result = subprocess.run([os.environ.get("NODE_BINARY", "node"),
        str(Path(__file__).resolve().parents[2] / "opencode_plugin/scope-test.mjs")],
        input=json.dumps({"scope": scope, "tool": tool, "args": args}),
        text=True, capture_output=True, check=False)
    if result.returncode:
        if "Invalid" in result.stderr:
            raise ValueError(result.stderr)
        raise PermissionError(result.stderr)
    return json.loads(result.stdout)


def publication(runtime, session_id, message_id, files):
    row = next(r for r in runtime.db.records("run") if r.get("session_id") == session_id)
    from agent_backend.models import Submit
    return {"session_id": session_id, "directory": str(runtime.workspace(Submit.model_validate(row["submit"]))),
            "message_id": message_id, "files": files}


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
        pass

    async def close(self):
        self.service.db.close()
