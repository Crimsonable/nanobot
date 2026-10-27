import time
from types import SimpleNamespace
from agent_backend.config import RuntimeSettings


def register_bucket(service, bucket_id="b", frontend_id="web"):
    service.config.runtime_templates[frontend_id] = RuntimeSettings(workspace_root=service.config.state_root / "users")
    service.config.frontend_idle_ttl_seconds[frontend_id] = 0
    service.db.put("bucket", bucket_id, {"bucket_id": bucket_id, "frontend_id": frontend_id,
        "status": "running", "created_at": time.time(), "last_active_at": time.time()})
    service.peers[bucket_id] = SimpleNamespace(closed=False)


def emit_answer(runtime, session_id, request_id, input_id, directory, message):
    from uuid import uuid4
    base = {"type": "opencode.event", "directory": directory}
    runtime.native_event({**base, "event_id": uuid4().hex, "event": {"type": "session.status",
        "properties": {"sessionID": session_id, "status": {"type": "busy"}}}})
    for i, part in enumerate(message["parts"]):
        runtime.native_event({**base, "event_id": uuid4().hex, "event": {"type": "message.part.updated",
            "properties": {"part": {**part, "id": str(i), "sessionID": session_id, "messageID": message["info"]["id"]}}}})
    event = {**base, "event_id": uuid4().hex, "event": {"type": "message.updated", "properties": {"info": message["info"]}}}
    runtime.native_event(event)
    if message["info"].get("time", {}).get("completed") and message["info"].get("finish") in {"stop", "end_turn", "length", "content-filter"}:
        runtime.native_event({**base, "event_id": uuid4().hex, "event": {"type": "session.status",
            "properties": {"sessionID": session_id, "status": {"type": "idle"}}}})
    while runtime.native_events.process_next():
        pass
    return event


def plugin_scope(runtime, session_id, call_id, tool, args):
    """Exercise TypeScript permissions with the scope returned by BU."""
    import json
    import os
    from pathlib import Path
    import subprocess
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
