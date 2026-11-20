"""V2 to V1 Web SSE projection; the only OpenCode version compatibility layer.

State is scoped to a request/session and committed with its native event and SSE
frames. Events without a V1 equivalent stay in the native audit log only.
"""
from copy import deepcopy
from hashlib import sha256


def part_id(value: str) -> str:
    return "prt_" + sha256(value.encode()).hexdigest()[:32]


def translate(event: dict, state: dict) -> list[dict]:
    kind, data = event["type"], event["data"]
    session, now = data.get("sessionID"), event.get("created", 0)
    messages, parts = state.setdefault("messages", {}), state.setdefault("parts", {})
    output = []

    def emit(name, properties):
        output.append({"id": f"{event['id']}:{len(output)}", "type": name, "properties": deepcopy(properties)})

    def message(info):
        emit("message.updated", {"info": info})

    def part(value):
        emit("message.part.updated", {"part": value})

    def status(value):
        emit("session.status", {"sessionID": session, "status": {"type": value}})

    if kind == "session.inbox.enqueued" and data["item"]["type"] in {"user", "synthetic"}:
        mid = data["inboxID"]
        state["input"] = mid
        info = {"id": mid, "sessionID": session, "role": "user", "time": {"created": now}}
        messages[mid] = info
        # V2 selects the effective agent/model at step start. Delay the V1 user
        # frame until those required fields are known, then emit it before the assistant.
        state.setdefault("pending_inputs", {})[mid] = {"id": part_id(f"{mid}:text"),
            "sessionID": session, "messageID": mid, "type": "text",
            "text": data["item"]["payload"]["text"], "synthetic": data["item"]["type"] == "synthetic"}
    elif kind == "session.inbox.delivered":
        state["input"] = data["inboxID"]
    elif kind == "session.step.started":
        mid, model = data["assistantMessageID"], data["model"]
        for input_id, text in state.pop("pending_inputs", {}).items():
            user = messages[input_id]
            user.update(agent=data["agent"], model={"providerID": model["providerID"], "modelID": model["id"]})
            message(user)
            part(text)
        info = {"id": mid, "sessionID": session, "role": "assistant", "parentID": state.get("input", ""),
                "agent": data["agent"], "mode": data["agent"], "providerID": model["providerID"],
                "modelID": model["id"], "time": {"created": data["started"]}, "cost": 0,
                "path": {"cwd": state.get("directory", ""), "root": state.get("directory", "")},
                "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}}
        messages[mid] = info
        message(info)
        part({"id": part_id(f"{mid}:step-start"), "sessionID": session, "messageID": mid, "type": "step-start"})
    elif kind in {"session.step.ended", "session.step.failed"}:
        mid = data["assistantMessageID"]
        info = messages.get(mid)
        if info is None:
            return []
        info["time"]["completed"] = now
        info.update(finish=data.get("finish", "error"), cost=data.get("cost", 0), tokens=data.get("tokens", info["tokens"]))
        if "error" in data:
            info["error"] = legacy_error(data["error"])
        part({"id": part_id(f"{mid}:step-finish"), "sessionID": session, "messageID": mid,
              "type": "step-finish", "reason": info["finish"], "cost": info["cost"], "tokens": info["tokens"]})
        message(info)
    elif kind.startswith(("session.text.", "session.reasoning.")):
        _, category, phase = kind.split(".")
        mid = data["assistantMessageID"]
        pid = part_id(f"{mid}:{category}:{data['ordinal']}")
        value = parts.setdefault(pid, {"id": pid, "sessionID": session, "messageID": mid,
                                       "type": category, "text": "", "time": {"start": now}})
        if phase == "delta":
            value["text"] += data["delta"]
            emit("message.part.delta", {"sessionID": session, "messageID": mid, "partID": pid,
                                        "field": "text", "delta": data["delta"]})
        else:
            if phase == "ended":
                value.update(text=data["text"], time={**value["time"], "end": now})
            part(value)
    elif kind.startswith("session.tool."):
        mid, call = data["assistantMessageID"], data["id"]
        pid = part_id(f"{mid}:tool:{call}")
        value = parts.get(pid)
        if kind == "session.tool.input.started":
            value = {"id": pid, "sessionID": session, "messageID": mid, "type": "tool", "callID": call,
                     "tool": {"shell": "bash", "patch": "apply_patch", "subagent": "task"}.get(data["name"], data["name"]),
                     "state": {"status": "pending", "input": {}, "raw": ""}}
            parts[pid] = value
        if value is None:
            return []
        tool = value["state"]
        if kind == "session.tool.input.delta":
            tool["raw"] = tool.get("raw", "") + data["delta"]
        elif kind == "session.tool.input.ended":
            tool["raw"] = data["text"]
        elif kind == "session.tool.called":
            inputs = dict(data["input"])
            if value["tool"] in {"read", "write", "edit"} and "path" in inputs:
                inputs["filePath"] = inputs.pop("path")
            if value["tool"] == "task":
                for source, target in [("agent", "subagent_type"), ("sessionID", "task_id")]:
                    if source in inputs:
                        inputs[target] = inputs.pop(source)
            value["state"] = {"status": "running", "input": inputs, "time": {"start": now}}
        elif kind == "session.tool.progress":
            tool["metadata"] = data["metadata"]
        elif kind in {"session.tool.success", "session.tool.failed"}:
            tool.update(status="completed" if kind.endswith("success") else "error",
                        time={**tool.get("time", {"start": now}), "end": now}, metadata=data.get("metadata", {}))
            if "error" in data:
                tool["error"] = data["error"].get("message", "Tool failed")
            else:
                tool["output"] = "\n".join(item["text"] for item in data["content"] if item["type"] == "text")
                tool["title"] = tool["metadata"].get("title", value["tool"])
                files = [{"id": part_id(f"{pid}:file:{i}"), "sessionID": session, "messageID": mid,
                          "type": "file", "mime": item["mime"], "url": item["uri"],
                          **({"filename": item["name"]} if item.get("name") else {})}
                         for i, item in enumerate(data["content"]) if item["type"] == "file"]
                if files:
                    tool["attachments"] = files
        part(value)
    elif kind == "session.execution.started":
        status("busy")
    elif kind in {"session.execution.succeeded", "session.execution.failed", "session.execution.interrupted"}:
        if "error" in data:
            emit("session.error", {"sessionID": session, "error": legacy_error(data["error"])})
        status("idle")
        emit("session.idle", {"sessionID": session})
    elif kind == "session.retry.scheduled":
        emit("session.status", {"sessionID": session, "status": {"type": "retry", "attempt": data["attempt"],
             "message": data["error"].get("message", "Retrying"), "next": data["at"]}})
    elif kind == "session.compaction.ended":
        emit("session.compacted", {"sessionID": session})
    elif kind == "permission.asked":
        request = {"id": data["id"], "sessionID": session, "permission": data["action"],
                   "patterns": data["resources"], "always": data.get("save", []), "metadata": data.get("metadata", {})}
        if data.get("source", {}).get("type") == "tool":
            request["tool"] = {"messageID": data["source"]["messageID"], "callID": data["source"]["id"]}
        emit("permission.asked", request)
    elif kind == "permission.replied":
        emit("permission.replied", data)
    elif kind == "form.created" and data["form"].get("metadata", {}).get("kind") == "question":
        form = data["form"]
        state.setdefault("questions", {})[form["id"]] = [field["key"] for field in form["fields"]]
        request = {"id": form["id"], "sessionID": form["sessionID"], "questions": [
            {"header": field.get("title", ""), "question": field.get("description", ""),
             "multiple": field["type"] == "multiselect", "options": [
                 {"label": option["label"], "description": option.get("description", "")}
                 for option in field.get("options", [])]} for field in form["fields"]]}
        if tool := form["metadata"].get("tool"):
            request["tool"] = {"messageID": tool["messageID"], "callID": tool["id"]}
        emit("question.asked", request)
    elif kind in {"form.replied", "form.cancelled"}:
        keys = state.get("questions", {}).pop(data["id"], None)
        if keys is not None:
            reply = {"requestID": data["id"], "sessionID": session}
            if kind == "form.replied":
                reply["answers"] = [value if isinstance(value, list) else [str(value)]
                                    for key in keys if (value := data["answer"].get(key, [])) is not None]
            emit("question.replied" if kind == "form.replied" else "question.rejected", reply)
    elif kind == "session.created":
        info = {"id": session, "directory": data["location"]["directory"], "projectID": data["projectID"],
                "title": data.get("title", ""), "slug": data["slug"], "version": data["version"],
                "time": {"created": now, "updated": now}, "metadata": data.get("metadata", {})}
        if data.get("parentID"):
            info["parentID"] = data["parentID"]
        state["session"] = info
        emit("session.created", {"info": info})
    elif kind in {"session.renamed", "session.metadata.updated", "session.moved", "session.archived",
                  "session.unarchived", "session.permissions", "session.deleted"} and "session" in state:
        info = state["session"]
        info["time"]["updated"] = now
        for key in ("title", "metadata"):
            if key in data:
                info[key] = data[key]
        if "location" in data:
            info["directory"] = data["location"]["directory"]
        if kind == "session.archived":
            info["time"]["archived"] = now
        elif kind == "session.unarchived":
            info["time"].pop("archived", None)
        elif kind == "session.permissions":
            info["permission"] = [{"permission": rule["action"], "pattern": rule["resource"],
                                   "action": rule["effect"]} for rule in data["permissions"]]
        emit("session.deleted" if kind.endswith("deleted") else "session.updated", {"info": info})
    return output


def legacy_error(error):
    if error.get("type") == "aborted":
        return {"name": "MessageAbortedError", "data": {"message": error["message"]}}
    return {"name": "UnknownError", "data": {"message": error.get("message") or error.get("type", "OpenCode error")}}
