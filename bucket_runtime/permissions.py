"""Compile a user's directory scope for OpenCode session creation."""
from __future__ import annotations

from pathlib import Path

from agent_backend.common import frontend_app_root, scoped_public_path
from agent_backend.config import RuntimeConfig, UserPolicy


def public_roots(config: RuntimeConfig, policy: UserPolicy) -> list[Path]:
    if not policy.sandbox.enabled or not policy.session.enabled:
        raise ValueError("frontend isolation requires sandbox.enabled=true and session.enabled=true")
    roots = policy.filesystem.public_roots or []
    # The frontend app tree is shared by every bucket/user of this frontend.
    # Policy overrides select additional public directories, never remove app.
    roots = [*roots, frontend_app_root(config.common_root, config.frontend_id)]
    protected = [config.workspace_root, config.state_root,
                 config.opencode.context_root, config.opencode.plugin_dir]
    private = [config.opencode.config_file, config.permissions_file, config.logging_file, config.frontend_config_file]
    result = []
    for root in roots:
        resolved = scoped_public_path(root, config.common_root, config.frontend_id)
        if resolved == Path(resolved.anchor):
            raise ValueError("filesystem root cannot be public")
        if any(resolved.is_relative_to(Path(p).resolve()) for p in ["/proc", "/sys", "/dev"]):
            raise ValueError("host process, kernel and device directories cannot be public")
        if any(resolved.is_relative_to(p.resolve()) or p.resolve().is_relative_to(resolved) for p in protected):
            raise ValueError("public roots must not expose users or service files")
        if any(p and resolved.is_relative_to(p.resolve()) for p in private):
            raise ValueError("service configuration cannot be a public root")
        if resolved not in result:
            result.append(resolved)
    return result


def session_rules(policy: UserPolicy, workspace: Path, public: list[Path],
                  private_files: list[Path] = ()) -> list[dict]:
    """V2 resources are location-relative internally and absolute outside it."""
    rules = []

    def rule(action, resource, allowed):
        rules.append({"action": action, "resource": resource, "effect": "allow" if allowed else "deny"})

    rule("*", "*", policy.session.default_tools)
    for name, enabled in policy.session.tools.items():
        rule(name, "*", enabled)
    readable = [*([workspace] if policy.filesystem.workspace != "none" else []), *public]
    writable = [workspace] if policy.filesystem.workspace == "read_write" else []
    for name, roots in [("read", readable), ("edit", writable), ("external_directory", readable)]:
        rule(name, "*", False)
        enabled = policy.session.tools.get(name, policy.session.default_tools)
        if name == "edit":
            enabled = any(policy.session.tools.get(tool, policy.session.default_tools)
                          for tool in ["edit", "write", "patch"])
        if name == "external_directory":
            enabled = policy.session.tools.get(name, True)
        if not enabled:
            continue
        for root in roots:
            if root == workspace and name != "external_directory":
                rule(name, "*", True)
                for resource in ("..", "../*", "/*", "*:*", "opencode://*"):
                    rule(name, resource, False)
            else:
                rule(name, root.as_posix(), True)
                rule(name, root.as_posix().rstrip("/") + "/*", True)
        # V2 hides a tool only when its last matching action rule denies "*".
        # A wholly disabled action needs no narrower private-file exceptions.
        if name in {"read", "edit"} and roots:
            for path in private_files:
                rule(name, path.resolve().as_posix(), False)
    if policy.session.tools.get("read", policy.session.default_tools):
        rule("read", "skill://*", True)
    return rules
