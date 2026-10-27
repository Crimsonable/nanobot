import { tool, type Plugin } from "@opencode-ai/plugin"
import { applyPermissions } from "./permissions"

function quote(value: string): string {
  return "'" + value.replaceAll("'", "'\\''") + "'"
}

// Decode only the exact single-quoted format emitted by this plugin. This is
// display/context normalization, never shell evaluation or an authorization.
function unwrap(command: unknown): { command: string; workdir: string } | undefined {
  if (typeof command !== "string" || !command.startsWith("exec '/usr/bin/flock' ")) return
  const words: string[] = []
  let i = 5
  while (i < command.length) {
    if (command[i++] !== "'") return
    let word = "", closed = false
    while (i < command.length) {
      if (command.slice(i, i + 4) === "'\\''") { word += "'"; i += 4 }
      else if (command[i] === "'") { i++; closed = true; break }
      else word += command[i++]
    }
    if (!closed) return
    words.push(word)
    if (i < command.length && command[i++] !== " ") return
  }
  const cwd = words.indexOf("--chdir")
  if (words[1] !== "--exclusive" || words[2] !== "--close" ||
      !words.includes("--unshare-all") || !words.includes("--clearenv") ||
      words.at(-4) !== "--" || words.at(-2) !== "-c" || cwd < 0) return
  return {command: words.at(-1)!, workdir: words[cwd + 1]}
}

export const BucketRuntimePlugin: Plugin = async ({ directory }) => {
  const url = process.env.BUCKET_RPC_URL
  const token = process.env.BUCKET_RPC_TOKEN
  if (!url || !token) throw new Error("Missing trusted runtime configuration")
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(url)) throw new Error("Runtime RPC must be loopback")

  async function rpc(path: string, body: unknown, signal?: AbortSignal) {
    const response = await fetch(url + path, {
      // Avoid reusing an idle loopback socket while Uvicorn is closing it.
      method: "POST", headers: { authorization: "Bearer " + token, "content-type": "application/json",
        connection: "close" },
      body: JSON.stringify(body), signal: signal ?? AbortSignal.timeout(650_000),
    })
    if (!response.ok) throw new Error(`Runtime rejected ${path}: ${response.status}`)
    return response.json()
  }

  await rpc("/plugin/ready", { version: "9" })
  return {
    "experimental.chat.messages.transform": async (_input, output) => {
      for (const message of output.messages) {
        for (const part of message.parts) {
          if (part.type !== "tool" || part.tool !== "bash") continue
          const original = unwrap(part.state.input?.command)
          if (original) part.state.input = {...part.state.input, ...original}
        }
      }
    },
    "tool.execute.before": async (input, output) => {
      // File/shell tools query BU once. Publication validates inside /publish.
      const args = structuredClone(output.args)
      if (input.tool === "publish_attachment") return
      const scope = await rpc("/permissions", {session_id: input.sessionID, directory})
      const authorized = applyPermissions(scope, input.tool, args)
      if (input.tool === "bash") {
        if (!Array.isArray(authorized.argv) || authorized.argv.length === 0 ||
            !authorized.argv.every((arg: unknown) => typeof arg === "string" && !arg.includes("\0")) ||
            typeof authorized.host_workdir !== "string" ||
            !Number.isFinite(authorized.timeout_ms) || authorized.timeout_ms <= 0) {
          throw new Error("Invalid sandbox authorization")
        }
        // Every argument is quoted separately: only the inner shell evaluates user text.
        // exec lets OpenCode own the sandbox process lifetime instead of a bridge client.
        output.args.command = "exec " + [...authorized.argv, args.command].map(quote).join(" ")
        output.args.workdir = authorized.host_workdir
        output.args.timeout = authorized.timeout_ms
      } else if (authorized.args) {
        Object.assign(output.args, authorized.args)
      }
    },
    tool: {
      publish_attachment: tool({
        description: "Upload generated files from your workspace directly to MinIO, return their object URLs, and include them in this reply. " +
          "Success means queued for delivery, not already delivered to the recipient.",
        args: {
          files: tool.schema.array(tool.schema.object({
            path: tool.schema.string(), filename: tool.schema.string().optional(),
          })).min(1).max(32),
        },
        async execute(args, context) {
          const result = await rpc("/publish", {
            session_id: context.sessionID, directory,
            message_id: context.messageID, files: args.files,
          }, context.abort)
          return JSON.stringify(result)
        },
      }),
    },
  }
}
