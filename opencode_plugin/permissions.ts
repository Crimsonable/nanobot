import * as fs from "node:fs"
import * as path from "node:path"

// BU returns the current scope; the plugin validates paths and builds commands.
function inside(target: string, root: string) {
  const relative = path.relative(root, target)
  return relative === "" || (!relative.startsWith(".." + path.sep) && relative !== ".." && !path.isAbsolute(relative))
}

function stat(value: string) {
  try { return fs.lstatSync(value) } catch (error: any) {
    if (error.code === "ENOENT") return undefined
    throw error
  }
}

function target(value: string, roots: string[], workspace: string, privateFiles: string[], search = false) {
  if (typeof value !== "string" || !value || value.includes("\0")) throw new Error("Invalid file path")
  const absolute = path.resolve(workspace, value)
  if (!roots.some(root => inside(absolute, root))) throw new Error("Path outside user permission scope")
  // Check existing ancestors too, including dangling links and paths for future files.
  for (let cursor = absolute; ; cursor = path.dirname(cursor)) {
    if (stat(cursor)?.isSymbolicLink()) throw new Error("Symbolic links are not accepted")
    if (path.dirname(cursor) === cursor) break
  }
  if (privateFiles.some(file => absolute === file || (search && inside(file, absolute))))
    throw new Error("Service configuration is not user content")
  const info = stat(absolute)
  if (info?.isFile() && info.nlink !== 1) throw new Error("Hard-linked files are not accepted")
  return absolute
}

export function applyPermissions(scope: any, tool: string, input: any) {
  const policy = scope.policy
  if (scope.version !== 1 || !policy?.session?.enabled || !policy?.sandbox?.enabled)
    throw new Error("Missing session isolation policy")
  if ((Object.hasOwn(policy.session.tools, tool) ? policy.session.tools[tool] : policy.session.default_tools) !== true) throw new Error("Tool disabled for this user")
  const access = policy.filesystem.workspace
  if (!["none", "read_only", "read_write"].includes(access)) throw new Error("Invalid workspace access")
  const workspace = path.resolve(scope.workspace)
  const publicRoots = scope.public_roots.map((p: string) => path.resolve(p))
  const privateFiles = scope.private_files.map((p: string) => path.resolve(p))
  // A directory replaced by a link after session creation must not change scope.
  for (const root of [workspace, ...publicRoots]) target(root, [root], workspace, [])
  const readable = [...publicRoots, ...(access === "none" ? [] : [workspace])]
  const writable = access === "read_write" ? [workspace] : []
  const args = structuredClone(input)
  const file = (value: string, write = false, search = false) =>
    target(value, write ? writable : readable, workspace, privateFiles, search)
  if (["read", "edit", "write", "lsp", "multiedit"].includes(tool)) {
    args.filePath = file(args.filePath, ["edit", "write", "multiedit"].includes(tool), tool === "lsp")
  } else if (["glob", "grep", "list"].includes(tool)) {
    args.path = file(args.path ?? workspace, false, tool === "grep")
    if (tool === "glob" && (typeof args.pattern !== "string" || path.isAbsolute(args.pattern) ||
        args.pattern.split(/[\\/]/).includes(".."))) throw new Error("Glob escapes its search directory")
  } else if (tool === "apply_patch") {
    if (typeof args.patchText !== "string") throw new Error("Invalid patch")
    args.patchText = args.patchText.replace(/^(\*\*\* (?:Add File|Update File|Delete File|Move to): )([^\r\n]+)/gm,
      (_: string, prefix: string, name: string) => prefix + file(name, true))
  } else if (tool === "bash") {
    if (typeof args.command !== "string" || args.command.length > 100000 || args.command.includes("\0"))
      throw new Error("Invalid shell command")
    const timeout = args.timeout ?? 120000
    if (typeof timeout !== "number" || !Number.isFinite(timeout) || timeout <= 0) throw new Error("Invalid shell timeout")
    const remaining = scope.deadline_at * 1000 - Date.now()
    if (!Number.isFinite(remaining) || remaining <= 0) throw new Error("Request deadline expired")
    const cwd = args.workdir === undefined ? (access === "none" ? "/" : workspace) : file(args.workdir)
    if (!fs.statSync(cwd).isDirectory()) throw new Error("Working directory is not a directory")
    const argv = ["/usr/bin/flock", "--exclusive", "--close", scope.lock_path,
      scope.sandbox.binary, "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
      "--clearenv", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/home/agent"]
    if (policy.sandbox.network) {
      argv.push("--share-net")
      if (fs.existsSync("/etc/resolv.conf")) argv.push("--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf")
    }
    const readonly = [...scope.sandbox.readonly_roots, ...publicRoots]
    for (const root of readonly) {
      if (fs.existsSync(root)) argv.push("--ro-bind", root, root)
      else if (publicRoots.includes(root)) throw new Error("Configured public directory is missing")
    }
    if (access !== "none") argv.push(access === "read_write" ? "--bind" : "--ro-bind", workspace, workspace)
    for (const privateFile of privateFiles) {
      if (readonly.some(root => inside(privateFile, root))) argv.push("--ro-bind", "/dev/null", privateFile)
    }
    argv.push("--chdir", cwd, "--setenv", "HOME", "/home/agent", "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
      "--setenv", "LANG", "C.UTF-8", "--setenv", "TMPDIR", "/tmp",
      "--setenv", "PLAYWRIGHT_BROWSERS_PATH", "/opt/toolchain/browsers", "--", scope.sandbox.shell, "-c")
    if (!argv.every(value => typeof value === "string" && value.length > 0 && !value.includes("\0")))
      throw new Error("Invalid sandbox scope")
    return {argv, host_workdir: workspace, timeout_ms: Math.max(1, Math.ceil(Math.min(timeout, remaining)))}
  }
  return {args}
}
