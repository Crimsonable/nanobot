import { tool, type Plugin } from "@opencode-ai/plugin"

function quote(value: string): string {
  return "'" + value.replaceAll("'", "'\\''") + "'"
}

export const BucketRuntimePlugin: Plugin = async () => {
  const url = process.env.BUCKET_RPC_URL
  const token = process.env.BUCKET_RPC_TOKEN
  const python = process.env.BUCKET_EXEC_PYTHON
  const client = process.env.BUCKET_EXEC_CLIENT
  if (!url || !token || !python || !client) throw new Error("Missing trusted runtime configuration")
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(url)) throw new Error("Runtime RPC must be loopback")

  async function rpc(path: string, body: unknown, signal?: AbortSignal) {
    const response = await fetch(url + path, {
      method: "POST", headers: { authorization: "Bearer " + token, "content-type": "application/json" },
      body: JSON.stringify(body), signal: signal ?? AbortSignal.timeout(650_000),
    })
    if (!response.ok) throw new Error(`Runtime rejected ${path}: ${response.status}`)
    return response.json()
  }

  await rpc("/plugin/ready", { version: "1" })
  return {
    "tool.execute.before": async (input, output) => {
      // Do not expose arbitrary tool execution even if another agent/config enables it.
      const args = structuredClone(output.args)
      delete args._runtime_ticket
      const authorized = await rpc("/authorize", {
        session_id: input.sessionID, call_id: input.callID, tool: input.tool, args,
      })
      if (input.tool === "bash") {
        output.args.command = [python, client, authorized.ticket].map(quote).join(" ")
        output.args.workdir = authorized.host_workdir
        // The sandbox owns the original timeout. Leave time for RPC/error cleanup.
        output.args.timeout = 650_000
      } else if (input.tool === "publish_attachment") {
        output.args._runtime_ticket = authorized.ticket
      } else {
        throw new Error("Tool is not supported by the runtime")
      }
    },
    "permission.ask": async (_input, output) => { output.status = "deny" },
    "command.execute.before": async () => { throw new Error("Direct commands are disabled") },
    tool: {
      publish_attachment: tool({
        description: "Upload generated files from your workspace and include them in this reply. " +
          "Success means queued for delivery, not already delivered to the recipient.",
        args: {
          files: tool.schema.array(tool.schema.object({
            path: tool.schema.string(), filename: tool.schema.string().optional(),
          })).min(1).max(32),
          caption: tool.schema.string().optional(),
          _runtime_ticket: tool.schema.string().optional().describe("Reserved; set by the runtime hook"),
        },
        async execute(args, context) {
          if (!args._runtime_ticket) throw new Error("Missing runtime authorization")
          const result = await rpc("/publish", { ticket: args._runtime_ticket }, context.abort)
          return JSON.stringify(result)
        },
      }),
    },
  }
}
