import * as fs from "node:fs"
import * as path from "node:path"
import { createHash } from "node:crypto"
import { parse } from "yaml"

export function configuration() {
  const config = JSON.parse(process.env.MANAGED_BUCKET ?? "null")
  if (!config?.frontend_id || !config.bucket_id)
    throw new Error("Missing managed bucket configuration")
  if (config.behavior === "pool" ? !config.permissions_file :
      config.behavior !== "individual" || !config.owner || !config.user_workspace)
    throw new Error("Missing managed bucket policy")
  return config
}

function inside(target: string, root: string) {
  const relative = path.relative(root, target)
  return relative === "" || (relative !== ".." && !relative.startsWith(".." + path.sep) && !path.isAbsolute(relative))
}

export function policyFor(file: string | null | undefined, frontend: string, user: string,
                          behavior: "pool" | "individual" = "pool", mounts: {name: string}[] = []) {
  // Read on use: policy edits apply to existing sessions without a watcher or RPC.
  const config = (file ? parse(fs.readFileSync(file, "utf8")) : {}) ?? {}
  const policy = {session: {enabled: true, default_tools: true, tools: {} as Record<string, boolean>},
    sandbox: {enabled: behavior === "pool", network: true},
    filesystem: {workspace: "read_write", public_roots: [] as string[], mounts: {} as Record<string, string>}}
  const seen = new Set<string>()
  for (const user of config.users ?? []) {
    const key = JSON.stringify([user.frontend_id ?? null, user.user_id])
    if (seen.has(key)) throw new Error("Duplicate user permission configuration")
    seen.add(key)
  }
  for (const patch of [config.defaults ?? {}, ...(config.users ?? []).filter((entry: any) =>
    (entry.frontend_id == null || entry.frontend_id === frontend) && entry.user_id === user)]) {
    for (const section of ["session", "sandbox", "filesystem"] as const) {
      if (!patch[section]) continue
      const tools = {...policy.session.tools, ...patch.session?.tools}
      const grants = {...policy.filesystem.mounts, ...patch.filesystem?.mounts}
      Object.assign(policy[section], patch[section])
      policy.session.tools = tools
      policy.filesystem.mounts = grants
    }
  }
  if (typeof policy.session.default_tools !== "boolean" || typeof policy.sandbox.network !== "boolean" ||
      !Object.values(policy.session.tools).every(value => typeof value === "boolean") ||
      !["none", "read_only", "read_write"].includes(policy.filesystem.workspace))
    throw new Error("Invalid user isolation policy")
  if (behavior === "pool") {
    if (!file || policy.session.enabled !== true || policy.sandbox.enabled !== true)
      throw new Error("Invalid user isolation policy")
  } else {
    if (policy.sandbox.network !== true) throw new Error("Individual native shell does not support sandbox.network=false")
    if (policy.filesystem.public_roots.length) throw new Error("Individual public directories must be declared in bucket.mounts")
    for (const [name, access] of Object.entries(policy.filesystem.mounts)) {
      if (!mounts.some(mount => mount.name === name) || !["none", "read_only", "read_write"].includes(access))
        throw new Error("Invalid individual mount permission")
    }
    policy.sandbox.enabled = false
  }
  return policy
}

async function inputFor(config: any, sessionID: string, before?: string) {
  let cursor: string | undefined, reached = before === undefined
  while (true) {
    const url = new URL(`/api/session/${encodeURIComponent(sessionID)}/message`, `http://127.0.0.1:${config.opencode.port}`)
    url.searchParams.set("limit", "200")
    if (cursor) url.searchParams.set("cursor", cursor)
    else url.searchParams.set("order", "desc")
    const response = await fetch(url, {headers: {authorization: "Basic " +
      Buffer.from("opencode:" + process.env.OPENCODE_PASSWORD).toString("base64")}, signal: AbortSignal.timeout(30_000)})
    if (!response.ok) throw new Error("Cannot read native request context")
    const page = await response.json()
    for (const message of page.data) {
      if (message.id === before) reached = true
      if (reached && message.type === "user") {
        const value = message.metadata?.managed
        if (!value?.request_id || !Number.isFinite(value.deadline_at)) throw new Error("Missing trusted input metadata")
        return {...value, input_message_id: message.id}
      }
    }
    cursor = page.cursor?.next
    if (!cursor) throw new Error("No managed native input")
  }
}

export async function scopeFor(ctx: any, config: any, sessionID: string, messageID?: string) {
  const directory = path.resolve(ctx.location.directory)
  let info = await ctx.session.get({sessionID})
  const seen = new Set<string>()
  while (info.parentID) {
    if (seen.has(info.id) || seen.size >= 64) throw new Error("Invalid session ancestry")
    seen.add(info.id)
    if (path.resolve(info.location.directory) !== directory) throw new Error("Child workspace mismatch")
    info = await ctx.session.get({sessionID: info.parentID})
  }
  const owner = info.metadata?.managed
  if (owner?.frontend_id !== config.frontend_id || typeof owner.user_id !== "string" || !owner.identity_id ||
      path.resolve(info.location.directory) !== directory ||
      !inside(directory, path.resolve(config.workspace_root, config.frontend_id)) ||
      directory === path.resolve(config.workspace_root, config.frontend_id))
    throw new Error("Missing managed root session")
  if (config.behavior === "individual") {
    const expected = config.owner
    const identity = createHash("sha256").update(JSON.stringify({frontend_id:expected.frontend_id,user_id:expected.user_id})).digest("hex")
    if (owner.frontend_id !== expected.frontend_id || owner.user_id !== expected.user_id ||
        owner.identity_id !== identity || directory !== path.resolve(config.user_workspace))
      throw new Error("Individual bucket owner mismatch")
    const input = await inputFor(config, sessionID, messageID)
    if (input.deadline_at * 1000 <= Date.now()) throw new Error("Request deadline expired")
    return {version:1, behavior:"individual", owner, root_session:info.id, request_id:input.request_id,
      workspace:directory, policy:policyFor(config.permissions_file, owner.frontend_id, owner.user_id, "individual", config.mounts ?? []),
      deadline_at:input.deadline_at,
      file_bytes:config.limits.file_bytes, spool_bytes:config.limits.spool_bytes}
  }
  const policy = policyFor(config.permissions_file, owner.frontend_id, owner.user_id)
  const privateFiles = [config.opencode.config_file, config.permissions_file, config.logging_file,
    config.frontend_config_file].filter(Boolean).map((value: string) => path.resolve(value))
  const app = path.resolve(config.common_root, config.frontend_id, "app")
  const protectedRoots = [config.workspace_root, config.state_root, config.opencode.context_root,
    config.opencode.plugin_dir, "/proc", "/sys", "/dev"].map((value: string) => path.resolve(value))
  const publicRoots = [...new Set([app, ...(policy.filesystem.public_roots ?? []).map((value: string) =>
    path.resolve(value.replaceAll("{frontend_id}", config.frontend_id)))])]
  for (const root of publicRoots) {
    if (root === path.parse(root).root || protectedRoots.some(value => inside(root, value) || inside(value, root)) ||
        (inside(root, path.resolve(config.common_root)) && !inside(root, app)) ||
        privateFiles.some((file: string) => inside(root, file))) throw new Error("Invalid public directory")
  }
  const input = await inputFor(config, sessionID, messageID)
  if (input.deadline_at * 1000 <= Date.now()) throw new Error("Request deadline expired")
  const lock = path.join(config.state_root, "locks", createHash("sha256").update(info.id).digest("hex") + ".lock")
  fs.mkdirSync(path.dirname(lock), {recursive: true})
  return {version: 1, behavior:"pool", owner, root_session: info.id, request_id: input.request_id,
    workspace: directory, policy, public_roots: publicRoots, private_files: privateFiles,
    sandbox: config.sandbox, deadline_at: input.deadline_at, lock_path: lock,
    file_bytes: config.limits.file_bytes, spool_bytes: config.limits.spool_bytes}
}
