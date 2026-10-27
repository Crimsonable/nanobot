"""Compile a user's directory scope for OpenCode session creation."""
from __future__ import annotations

import os
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
                 config.opencode.context_root, config.opencode.plugin_file,
                 config.opencode.dependency_dir]
    private = [config.opencode.config_file, config.permissions_file, config.logging_file, config.frontend_config_file]
    if config.storage:
        private.extend([config.storage.access_key_file, config.storage.secret_key_file])
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


def session_rules(policy: UserPolicy, workspace: Path, public: list[Path], worktree: Path,
                  private_files: list[Path] = ()) -> list[dict]:
    if not policy.session.enabled:
        return []
    rules = []

    def rule(permission: str, pattern: str, allowed: bool):
        rules.append({"permission": permission, "pattern": pattern, "action": "allow" if allowed else "deny"})

    rule("*", "*", policy.session.default_tools)
    for name, enabled in policy.session.tools.items():
        rule(name, "*", enabled)
    readable = [*public, *([workspace] if policy.filesystem.workspace != "none" else [])]
    writable = [workspace] if policy.filesystem.workspace == "read_write" else []
    # read/edit ask with paths relative to the actual OpenCode worktree. External
    # directory permission asks with absolute directory globs, independently.
    for name, roots in [("read", readable), ("edit", writable), ("external_directory", readable)]:
        rule(name, "*", False)
        enabled = policy.session.tools.get(name, policy.session.default_tools)
        if name == "edit":
            enabled = any(policy.session.tools.get(tool, policy.session.default_tools)
                          for tool in ["edit", "write", "apply_patch", "multiedit"])
        if name == "external_directory":
            enabled = policy.session.tools.get(name, True)
        if not enabled:
            continue
        # The workspace may itself be the Git worktree. Its relative children
        # have no './' prefix; deny parent traversal before adding public roots.
        for root in sorted(roots, key=lambda root: root != worktree):
            pattern = (str(root) if name == "external_directory" else os.path.relpath(root, worktree)).replace("\\", "/")
            rule(name, pattern, True)
            if pattern == "." and name != "external_directory":
                rule(name, "*", True)
                rule(name, "..", False)
                rule(name, "../*", False)
                rule(name, "/*", False)
                continue
            rule(name, pattern.rstrip("/") + "/*", True)
    for path in private_files:
        pattern = os.path.relpath(path, worktree).replace("\\", "/")
        rule("read", pattern, False)
        rule("edit", pattern, False)
    return rules
