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
        if not cfg.enabled:
            return None
        native = event.event
        kind, data = native["type"], native["data"]
        content = ""
        if kind == "session.text.ended" and cfg.text:
            content = data["text"]
        elif kind == "session.reasoning.ended" and cfg.reasoning:
            content = data["text"]
        elif kind == "session.tool.called" and cfg.tools and cfg.tool_arguments:
            content = data["input"]
        elif kind in {"session.tool.success", "session.tool.failed"} and cfg.tools and cfg.tool_output:
            content = data.get("content") or data.get("error", "")
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
