"""Compact runtime-owned session output; never dump native event metadata."""
import json
import re

from agent_backend.config import SessionLoggingConfig, load_config

SECRET = re.compile(r"^(?:authorization|cookie|api[-_]?key|access[-_]?key|secret[-_]?key|password|token|access_token|refresh_token|client_secret)$", re.I)


def redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if SECRET.fullmatch(k) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"\bBearer\s+[^\s\"']+", "Bearer [REDACTED]", value, flags=re.I)
    return value


class SessionLogger:
    def __init__(self, path=None, write=None):
        self.settings = load_config(path, SessionLoggingConfig) if path else SessionLoggingConfig()
        self.write = write or (lambda line: print(line, flush=True))

    def format(self, event, owner):
        cfg = self.settings
        if not cfg.enabled or event.event.get("type") != "message.part.updated":
            return None
        native = event.event
        props = native.get("properties", {})
        part = props.get("part") or {}
        part_type = part.get("type")
        if ((part_type == "text" and not cfg.text) or
                (part_type == "reasoning" and not cfg.reasoning) or
                (part_type == "tool" and not cfg.tools)):
            return None
        content = ""
        if part_type in {"text", "reasoning"}:
            content = part.get("text", "")
        elif part_type == "tool" and cfg.tool_output:
            state = part.get("state", {})
            content = {k: state[k] for k in ("output", "error") if k in state} or ""
        if content == "":
            return None
        content = redact(content)
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        if len(content) > cfg.max_chars:
            content = content[:cfg.max_chars - 1] + "…"
        return json.dumps({"user_id": owner.get("user_id", ""),
            "frontend_id": owner.get("frontend_id", ""), "chat_id": owner.get("chat_id", ""),
            "content": content}, ensure_ascii=False, separators=(",", ":"))
