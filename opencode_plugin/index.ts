import type { Plugin } from "@opencode/plugin"
import { applyPermissions, visibleSkill, workspaceInstructions } from "./permissions.js"

function quote(value: string): string {
  return "'" + value.replaceAll("'", "'\\''") + "'"
}

// Decode only the exact single-quoted format emitted by this plugin. This is
// display/context normalization, never shell evaluation or an authorization.
function unwrap(command: unknown): { command: string; workdir: string } | undefined {
  if (typeof command !== "string" || !command.startsWith("exec '/usr/bin/timeout' ")) return
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
  if (words[0] !== "/usr/bin/timeout" || words[1] !== "--kill-after=1s") return
  const sandbox = words.slice(3)
  const cwd = sandbox.indexOf("--chdir")
  if (sandbox[1] !== "--exclusive" || sandbox[2] !== "--close" ||
      !sandbox.includes("--unshare-all") || !sandbox.includes("--clearenv") ||
      sandbox.at(-4) !== "--" || sandbox.at(-2) !== "-c" || cwd < 0) return
  return {command: sandbox.at(-1)!, workdir: sandbox[cwd + 1]}
}

export default {
  id: "bucket-runtime",
  async setup(ctx) {
    const directory = ctx.location.directory
    const url = process.env.BUCKET_RPC_URL
    const token = process.env.BUCKET_RPC_TOKEN
    const publicRoot = process.env.BUCKET_PUBLIC_ROOT
    if (!url || !token || !publicRoot) throw new Error("Missing trusted runtime configuration")
    if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(url)) throw new Error("Runtime RPC must be loopback")

    async function rpc(path: string, body: unknown, signal?: AbortSignal) {
      const response = await fetch(url + path, {
        method: "POST", headers: { authorization: "Bearer " + token, "content-type": "application/json",
          connection: "close" },
        body: JSON.stringify(body), signal: signal ?? AbortSignal.timeout(650_000),
      })
      if (!response.ok) throw new Error(`Runtime rejected ${path}: ${response.status}`)
      return response.json()
    }

    await ctx.skill.transform(editor => {
      for (const skill of editor.list()) {
        if (!visibleSkill(skill.path, directory, publicRoot)) editor.remove(skill.id)
      }
    })
    await ctx.session.hook("experimental.idle", async event => {
      // V2 may finish a foreground execution while background children still work.
      // Their notices resume native execution; Gateway must wait for that final turn.
      await rpc("/session/idle", {session_id: event.sessionID, directory,
        background: event.background.length > 0})
    })
    for (const hook of ["context", "compaction", "generate"] as const) {
      await ctx.session.hook(hook, event => {
        const instructions = workspaceInstructions(directory)
        if (instructions) event.system.push({type: "text", text: instructions})
      })
    }
    await ctx.session.hook("context", event => {
      event.messages = event.messages.map(message => ({...message,
        content: message.content.map(part => {
          if (part.type !== "tool-call" || part.name !== "shell" ||
              !part.input || typeof part.input !== "object") return part
          const input = part.input as Record<string, unknown>
          const original = unwrap(input.command)
          return original ? {...part, input: {...input, ...original}} : part
        }),
      }))
    })
    await ctx.tool.hook("execute.before", async event => {
      if (event.tool === "publish_attachment") return
      const args = structuredClone(event.input) as Record<string, any>
      const scope = await rpc("/permissions", {session_id: event.sessionID, directory})
      const authorized = applyPermissions(scope, event.tool, args)
      if (event.tool === "shell") {
        // Native shell owns execution/cancellation; a hard deadline ends the sandbox
        // before V2 would background a foreground command on its own timeout.
        const seconds = (authorized.timeout_ms / 1000).toFixed(3) + "s"
        event.input = {...args, command: "exec " + ["/usr/bin/timeout", "--kill-after=1s", seconds,
          ...authorized.argv, args.command].map(quote).join(" "),
          workdir: authorized.host_workdir, timeout: authorized.timeout_ms + 2000, background: false}
      } else {
        event.input = authorized.args
      }
    })
    await ctx.tool.transform(editor => {
      // V2 management tools have individual effective IDs in this namespace.
      // Remove them from both direct tools and the Code Mode catalogue.
      for (const tool of editor.list()) {
        if (tool.options?.namespace === "opencode") editor.remove(tool.id)
      }
      editor.add({
        name: "publish_attachment",
        // V2 places custom tools in Code Mode by default. Publication remains a
        // directly callable tool with the current session/message context.
        options: {codemode: false},
        description: "Publish generated workspace files through Gateway for the current channel. " +
          "Only use a URL when one is returned. Success means queued for delivery.",
        input: {type: "object", properties: {files: {type: "array", minItems: 1, maxItems: 32,
          items: {type: "object", properties: {path: {type: "string"}, filename: {type: "string"}},
            required: ["path"], additionalProperties: false}}}, required: ["files"], additionalProperties: false},
        async execute(input, context) {
          const result = await rpc("/publish", {session_id: context.sessionID, directory,
            message_id: context.messageID, files: (input as {files: unknown[]}).files}, context.signal)
          return {content: JSON.stringify(result)}
        },
      })
    })
    await rpc("/plugin/ready", {version: "10"})
  },
} satisfies Plugin.Plugin
