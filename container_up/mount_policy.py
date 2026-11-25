"""Resolve individual filesystem grants once when deploying the bucket."""
from __future__ import annotations

import yaml

from agent_backend.config import BucketConfig


def individual_policy(config: BucketConfig) -> dict:
    policy = {"session": {"enabled": True, "default_tools": True, "tools": {}},
              "filesystem": {"workspace": "read_write", "mounts": {}},
              "sandbox": {"enabled": False, "network": True}}
    raw = yaml.safe_load(config.permissions_file.read_text(encoding="utf-8")) if config.permissions_file else {}
    raw = raw or {}
    users = raw.get("users", [])
    seen = set()
    for entry in users:
        key = (entry.get("frontend_id"), entry["user_id"])
        if key in seen:
            raise ValueError("duplicate user permission configuration")
        seen.add(key)
    patches = [raw.get("defaults", {}), *(u for u in users if u["user_id"] == config.owner.user_id
               and u.get("frontend_id", config.frontend_id) in (None, config.frontend_id))]
    for patch in patches:
        for section in policy:
            values = patch.get(section, {})
            for key, value in values.items():
                if key in {"tools", "mounts"}:
                    policy[section][key] = {**policy[section].get(key, {}), **value}
                else:
                    policy[section][key] = value
    access = {"read_write", "read_only", "none"}
    if policy["filesystem"]["workspace"] not in access:
        raise ValueError("invalid individual workspace access")
    mounts = policy["filesystem"]["mounts"]
    if set(mounts) - {m.name for m in config.mounts} or any(v not in access for v in mounts.values()):
        raise ValueError("invalid individual mount permission")
    if policy["filesystem"].get("public_roots"):
        raise ValueError("individual public directories must be declared in bucket.mounts")
    if policy["sandbox"]["network"] is not True:
        raise ValueError("individual native shell does not support sandbox.network=false")
    session = policy["session"]
    if type(session["default_tools"]) is not bool or any(type(v) is not bool for v in session["tools"].values()):
        raise ValueError("invalid individual tool configuration")
    policy["sandbox"]["enabled"] = False
    return policy
