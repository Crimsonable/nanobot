"""Compile a user's directory scope for OpenCode and validate native file targets."""
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


def check_native_paths(tool: str, args: dict, policy: UserPolicy, workspace: Path, public: list[Path],
                       private_files: list[Path] = ()) -> dict:
    """Return normalized arguments; no model-supplied directory is trusted."""
    if not policy.session.enabled:
        return args
    if not policy.session.tools.get(tool, policy.session.default_tools):
        raise PermissionError("tool disabled for this user")
    readable = [*public, *([workspace] if policy.filesystem.workspace != "none" else [])]
    writable = [workspace] if policy.filesystem.workspace == "read_write" else []

    def target(value: str, write: bool) -> str:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        candidate = Path(os.path.abspath(candidate))
        roots = writable if write else readable
        root = next((root for root in roots if candidate.is_relative_to(root)), None)
        if root is None:
            raise PermissionError("path outside user permission scope")
        for part in [candidate, *candidate.parents]:
            if part.is_symlink():
                raise PermissionError("symbolic links are not accepted by native file hooks")
            if part == root:
                break
        resolved = candidate.resolve()
        for private in private_files:
            if resolved == private.resolve() or (tool in {"grep", "lsp"} and private.resolve().is_relative_to(resolved)):
                raise PermissionError("service configuration is not user content")
        if not resolved.is_relative_to(root):
            raise PermissionError("path outside user permission scope")
        if resolved.is_file() and resolved.stat().st_nlink != 1:
            raise PermissionError("hard-linked files are not accepted by native file hooks")
        return str(resolved)

    if tool == "publish_attachment" and policy.filesystem.workspace == "none":
        raise PermissionError("workspace access is disabled")
    args = dict(args)
    if tool in {"read", "edit", "write", "lsp"}:
        args["filePath"] = target(args["filePath"], tool in {"edit", "write"})
    elif tool in {"glob", "grep", "list"}:
        args["path"] = target(args.get("path") or str(workspace), False)
        if tool == "glob" and (Path(args["pattern"]).is_absolute() or ".." in Path(args["pattern"]).parts):
            raise PermissionError("glob pattern must stay within its search directory")
    elif tool == "multiedit":
        args["filePath"] = target(args["filePath"], True)
    elif tool == "apply_patch":
        lines = args["patchText"].splitlines(keepends=True)
        prefixes = ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: ")
        for index, line in enumerate(lines):
            for prefix in prefixes:
                if line.startswith(prefix):
                    lines[index] = prefix + target(line[len(prefix):].rstrip("\r\n"), True) + "\n"
                    break
        args["patchText"] = "".join(lines)
    return args
