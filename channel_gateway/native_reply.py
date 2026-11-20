"""Assemble final replies from received V2 events without querying OpenCode."""
from agent_backend.models import Usage


def resolve_reply(db, bucket_id, source, result):
    steps = {}
    usage = Usage()
    for record in db.records("opencode_event"):
        payload = record["payload"]
        if (record["bucket_id"] != bucket_id or record["request_id"] != result.request_id
                or payload.get("session_id") != source["session_id"]):
            continue
        event = payload["event"]
        kind, data = event["type"], event["data"]
        message = data.get("assistantMessageID")
        if kind == "session.step.started":
            steps[message] = {"texts": {}, "finish": None}
        elif kind == "session.text.ended" and message in steps:
            steps[message]["texts"][data["ordinal"]] = data["text"]
        elif kind in {"session.step.ended", "session.step.failed"} and message in steps:
            steps[message]["finish"] = data.get("finish")
            tokens = data.get("tokens") or {}
            usage.input_tokens += tokens.get("input", 0)
            usage.output_tokens += tokens.get("output", 0)
            usage.reasoning_tokens += tokens.get("reasoning", 0)
            usage.cache_read_tokens += tokens.get("cache", {}).get("read", 0)
            usage.cache_write_tokens += tokens.get("cache", {}).get("write", 0)
    final = next(reversed(steps.values()), None) if steps else None
    if final is None or final["finish"] in {None, "tool-calls"}:
        return result.model_copy(update={"status": "failed", "error": result.error or "NATIVE_REPLY_INCOMPLETE",
                                          "partial": bool(result.attachments), "usage": usage})
    content = "\n".join(final["texts"][key] for key in sorted(final["texts"]))
    error = result.error or (None if content or result.attachments else "EMPTY_RESULT")
    return result.model_copy(update={"content": content, "error": error,
        "status": "failed" if error else "completed", "partial": bool(error and (content or result.attachments)),
        "usage": usage})
