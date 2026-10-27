"""Use native events already received by Gateway; never query OpenCode for replies."""
from channel_gateway.session_results import final_message, usage_for


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
        elif event["type"] == "message.removed":
            messages.pop(props["messageID"], None)
        elif event["type"] == "message.part.removed":
            messages.get(props["messageID"], {}).get("parts", {}).pop(props["partID"], None)
        elif event["type"] == "message.part.delta":
            part = messages.get(props["messageID"], {}).get("parts", {}).get(props["partID"])
            if part is not None and props.get("field") == "text":
                part["text"] = part.get("text", "") + props.get("delta", "")
        elif event["type"] == "message.part.updated":
            part = props["part"]
            message = messages.setdefault(part["messageID"], {"info": {}, "parts": {}})
            message["parts"][part["id"]] = part
    snapshots = [{"info": m["info"], "parts": list(m["parts"].values())} for m in messages.values()]
    final = final_message(snapshots, source["input_message_id"])
    if final is None:
        return result.model_copy(update={"status": "failed", "error": "NATIVE_REPLY_INCOMPLETE",
                                          "partial": bool(result.attachments)})
    content, error = final
    error = result.error or error or (None if content or result.attachments else "EMPTY_RESULT")
    return result.model_copy(update={"content": content, "error": error,
        "status": "failed" if error else "completed", "partial": bool(error and result.attachments),
        "usage": usage_for(snapshots, source["input_message_id"])})
