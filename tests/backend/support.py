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
    base = {"type": "opencode.event", "session_id": session_id, "request_id": request_id,
            "input_message_id": input_id, "directory": directory, "message_id": message["info"]["id"]}
    for i, part in enumerate(message["parts"]):
        runtime.plugin_event({**base, "event_id": uuid4().hex, "event": {"type": "message.part.updated",
            "properties": {"part": {**part, "id": str(i), "sessionID": session_id, "messageID": base["message_id"]}}}})
    event = {**base, "event_id": uuid4().hex, "event": {"type": "message.updated", "properties": {"info": message["info"]}}}
    runtime.plugin_event(event)
    return event
