import type { Plugin } from "@opencode/plugin"
import { configuration, scopeFor } from "./policy.js"
import { publication } from "./publication.js"
import * as path from "node:path"
import * as fs from "node:fs"
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
  id: "managed-bucket",
  async setup(ctx) {
    const directory = ctx.location.directory
    const config = configuration()
    const publicRoot = path.join(config.common_root, config.frontend_id, "app")

    if (config.behavior === "pool") await ctx.skill.transform(editor => {
      for (const skill of editor.list()) {
        if (!visibleSkill(skill.path, directory, publicRoot)) editor.remove(skill.id)
      }
    })
    await ctx.session.hook("prompt", async event => {
      const info = await ctx.session.get({sessionID: event.sessionID})
      if (!info.parentID) return
      const scope = await scopeFor(ctx, config, info.parentID)
      // Native subagent prompts carry the caller's request context, including
      // follow-ups to an existing child. This metadata is never model text.
      event.metadata = {...event.metadata, managed: {request_id: scope.request_id, deadline_at: scope.deadline_at}}
    })
    for (const hook of ["context", "compaction", "generate"] as const) {
      await ctx.session.hook(hook, async event => {
        const scope = await scopeFor(ctx, config, event.sessionID)
        const readable = scope.policy.filesystem.workspace !== "none" &&
          (scope.policy.session.tools.read ?? scope.policy.session.default_tools)
        const instructions = readable ? (config.behavior === "pool" ? workspaceInstructions(directory)
          : fs.existsSync(path.join(directory,"AGENTS.md")) ? fs.readFileSync(path.join(directory,"AGENTS.md"),"utf8") : undefined) : undefined
        if (instructions) event.system.push({type: "text", text: instructions})
        for (const name of Object.keys(event.tools)) {
          if (!(scope.policy.session.tools[name] ?? scope.policy.session.default_tools) ||
              (config.behavior === "pool" && scope.policy.filesystem.workspace !== "read_write" && ["write", "edit", "patch"].includes(name)) ||
              (scope.policy.filesystem.workspace === "none" && name === "publish_attachment")) delete event.tools[name]
        }
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
    await ctx.permission.hook("evaluate", async event => {
      try {
        const scope = await scopeFor(ctx, config, event.sessionID)
        const policy = scope.policy.session
        const enabled = event.action === "external_directory" || event.action === "edit"
          ? true : (policy.tools[event.action] ?? policy.default_tools)
        event.effect = enabled && !event.action.startsWith("opencode_") ? "allow" : "deny"
      } catch { event.effect = "deny" }
    })
    await ctx.tool.hook("execute.before", async event => {
      if (event.tool === "publish_attachment") return
      const args = structuredClone(event.input) as Record<string, any>
      const scope = await scopeFor(ctx, config, event.sessionID, event.messageID)
      if (config.behavior === "individual") {
        if (event.tool.startsWith("opencode_")) throw new Error("Server control is not available to managed sessions")
        if (!(scope.policy.session.tools[event.tool] ?? scope.policy.session.default_tools))
          throw new Error("Tool disabled for this user")
        // The native tool owns its command, workdir, timeout and cancellation unchanged.
        return
      }
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
          const scope = await scopeFor(ctx, config, context.sessionID, context.messageID)
          const result = await publication(config, scope, (input as {files: unknown[]}).files, context)
          return {content: JSON.stringify(result)}
        },
      })
    })
  },
} satisfies Plugin.Plugin
