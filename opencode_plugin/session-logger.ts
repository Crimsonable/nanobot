type Settings = {
  enabled: boolean; text: boolean; reasoning: boolean; tools: boolean
  tool_arguments: boolean; tool_output: boolean; max_chars: number
}
const secretKey = /^(?:_runtime_ticket|authorization|cookie|api[-_]?key|access[-_]?key|secret[-_]?key|password|token|access_token|refresh_token|client_secret)$/i

// Logging consumes the same attributed envelope as transport, with no caches or API lookups.
export function createSessionLogger(settings: Settings,
  write: (line: string) => void = line => { process.stdout.write(line + "\n") }) {
  return (body: any, owner: any) => {
    if (!settings.enabled) return
    const event = body.event, p = event.properties ?? {}, part = p.part
    const type = owner.part_type
    if (type === "text" && !settings.text || type === "reasoning" && !settings.reasoning) return
    if ((type === "tool" || event.type === "tool.execute.before") && !settings.tools) return
    const data = structuredClone(p)
    if (!settings.tool_arguments) {
      delete data.args
      if (data.part?.state) delete data.part.state.input
    }
    if (!settings.tool_output && data.part?.state) delete data.part.state.output
    const text = JSON.stringify(data, (key, value) => secretKey.test(key) ? "[REDACTED]" : value)
      .replace(/\bBearer\s+[^\s"']+/gi, "Bearer [REDACTED]")
    write(JSON.stringify({ timestamp: new Date().toISOString(), logger: "session", event: event.type,
      event_id: body.event_id, session_id: body.session_id, message_id: body.message_id,
      request_id: body.request_id, frontend_id: owner.frontend_id, user_id: owner.user_id,
      chat_id: owner.chat_id, bucket_id: owner.bucket_id, parent_session_id: owner.parent_session_id,
      data: text.slice(0, settings.max_chars), truncated: text.length > settings.max_chars }))
  }
}
