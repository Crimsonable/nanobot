"""Validate final reply snapshots pushed by the OpenCode plugin."""
from agent_backend.models import Usage


def final_message(messages: list[dict], input_id: str) -> tuple[str, str | None] | None:
    """Only a completed assistant answer correlated to this input may terminate a run."""
    answers = [m for m in messages if m["info"].get("role") == "assistant"
               and m["info"].get("parentID") == input_id and not m["info"].get("summary")]
    if not answers:
        return None
    if any(p.get("type") == "tool" and p.get("state", {}).get("status") in {"pending", "running"}
           for m in answers for p in m.get("parts", [])):
        return None
    last = max(answers, key=lambda m: (m["info"].get("time", {}).get("created", 0), m["info"]["id"]))
    info = last["info"]
    if not info.get("time", {}).get("completed"):
        return None
    if info.get("error"):
        return "", str(info["error"].get("name", "MODEL_ERROR"))
    if info.get("finish") not in {"stop", "end_turn", "length", "content-filter"}:
        return None
    text = "\n".join(p["text"] for p in last.get("parts", []) if p.get("type") == "text"
                     and not p.get("ignored") and p.get("text"))
    return text, None if info["finish"] in {"stop", "end_turn"} else "MODEL_" + info["finish"].upper()


def usage_for(messages: list[dict], input_id: str) -> Usage:
    result = Usage()
    for message in messages:
        info = message["info"]
        if info.get("role") != "assistant" or info.get("parentID") != input_id:
            continue
        tokens = info.get("tokens", {})
        result.input_tokens += int(tokens.get("input", 0))
        result.output_tokens += int(tokens.get("output", 0))
        result.reasoning_tokens += int(tokens.get("reasoning", 0))
        result.cache_read_tokens += int(tokens.get("cache", {}).get("read", 0))
        result.cache_write_tokens += int(tokens.get("cache", {}).get("write", 0))
    return result
