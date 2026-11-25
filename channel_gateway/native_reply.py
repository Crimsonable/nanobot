"""Incremental native reply projection owned by Gateway."""
from agent_backend.models import Result, Usage, digest


def snapshot_events(session_id, messages):
    """Project native message snapshots through the same Web event adapter."""
    for message in messages:
        created = message["time"]["created"]
        def event(kind, data, when=created):
            data = {"sessionID": session_id, **data}
            return {"id": "snapshot_" + digest([message["id"], kind, data]),
                    "type": kind, "created": when, "data": data}
        kind = message["type"]
        if kind in {"user", "synthetic"}:
            yield event("session.inbox.enqueued", {"inboxID":message["id"],
                "item":{"type":kind, "payload":{"text":message["text"]}}})
            yield event("session.inbox.delivered", {"inboxID":message["id"]})
        elif kind == "idle":
            yield event("session.execution." + message["outcome"],
                        {"error":message["error"]} if message.get("error") else {})
        elif kind == "assistant":
            base = {"assistantMessageID":message["id"]}
            yield event("session.step.started", {**base, "started":created,
                        "agent":message["agent"], "model":message["model"]})
            completed = message["time"].get("completed")
            if completed is None:
                continue
            ordinal = {"text":0, "reasoning":0}
            for part in message["content"]:
                category = part["type"]
                if category in ordinal:
                    yield event(f"session.{category}.ended", {**base,
                        "ordinal":ordinal[category], "text":part["text"]}, completed)
                    ordinal[category] += 1
                elif category == "tool":
                    tool = {**base,"id":part["id"]}
                    state = part["state"]
                    yield event("session.tool.input.started", {**tool,"name":part["name"]})
                    if state["status"] != "streaming":
                        yield event("session.tool.called", {**tool,"input":state["input"]})
                    if state["status"] in {"completed", "error"}:
                        payload = {"error":state["error"]} if state["status"] == "error" else {"content":state["content"]}
                        yield event("session.tool.failed" if state["status"] == "error" else "session.tool.success",
                                    {**tool,**payload,"metadata":state.get("metadata",{})}, completed)
            end = {**base,"tokens":message.get("tokens",{}),"cost":message.get("cost",0)}
            if message.get("error"):
                yield event("session.step.failed", {**end,"error":message["error"]}, completed)
            else:
                yield event("session.step.ended", {**end,"finish":message.get("finish")}, completed)


def accumulate(state: dict, event: dict) -> None:
    kind, data = event["type"], event["data"]
    steps = state.setdefault("steps", {})
    message = data.get("assistantMessageID")
    if kind.startswith("session.execution."):
        state["execution"] = kind
    if kind == "session.step.started":
        steps[message] = {"texts": {}, "finish": None}
    elif kind == "session.text.ended" and message in steps:
        steps[message]["texts"][str(data["ordinal"])] = data["text"]
    elif kind in {"session.step.ended", "session.step.failed"} and message in steps:
        steps[message]["finish"] = data.get("finish", "error")
        tokens = data.get("tokens") or {}
        usage = state.setdefault("usage", Usage().model_dump())
        for name, value in {"input_tokens": tokens.get("input", 0),
                "output_tokens": tokens.get("output", 0), "reasoning_tokens": tokens.get("reasoning", 0),
                "cache_read_tokens": tokens.get("cache", {}).get("read", 0),
                "cache_write_tokens": tokens.get("cache", {}).get("write", 0)}.items():
            usage[name] += value


def result_for(control: dict, state: dict, attachments: list) -> Result:
    result = Result(**control, attachments=[] if control["status"] == "cancelled" else attachments)
    if result.status != "completed" or result.error:
        return result.model_copy(update={"partial": bool(result.attachments)})
    steps = state.get("steps", {})
    final = next(reversed(steps.values()), None) if steps else None
    usage = Usage.model_validate(state.get("usage", {}))
    if (state.get("execution") != "session.execution.succeeded" or final is None
            or final["finish"] in {None, "tool-calls", "error"}):
        return result.model_copy(update={"status": "failed", "error": "NATIVE_REPLY_INCOMPLETE",
                                         "partial": bool(attachments), "usage": usage})
    content = "\n".join(final["texts"][key] for key in sorted(final["texts"], key=int))
    error = None if content or attachments else "EMPTY_RESULT"
    return result.model_copy(update={"content": content, "usage": usage, "error": error,
                                     "status": "failed" if error else "completed"})
