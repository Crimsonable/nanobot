"""Use native events already received by CU; never query OpenCode for replies."""
from container_up.session_results import final_message, usage_for


def resolve_reply(db, bucket_id, source, result):
    messages = {}
    for record in db.records("opencode_event"):
        payload = record["payload"]
        if record["bucket_id"] != bucket_id or payload.get("session_id") != source["session_id"]:
            continue
        event = payload["event"]
        props = event.get("properties", {})
        if event["type"] == "message.updated":
            info = props["info"]
            message = messages.setdefault(info["id"], {"info": {}, "parts": {}})
            message["info"] = info
        elif event["type"] == "message.part.updated":
            part = props["part"]
            message = messages.setdefault(part["messageID"], {"info": {}, "parts": {}})
            message["parts"][part["id"]] = part
    snapshots = [{"info": m["info"], "parts": list(m["parts"].values())} for m in messages.values()]
    final = final_message(snapshots, source["input_message_id"])
    if final is None:
        raise ValueError("terminal native reply is not complete in received events")
    content, error = final
    error = result.error or error or (None if content or result.attachments else "EMPTY_RESULT")
    return result.model_copy(update={"content": content, "error": error,
        "status": "failed" if error else "completed", "partial": bool(error and result.attachments),
        "usage": usage_for(snapshots, source["input_message_id"])})
