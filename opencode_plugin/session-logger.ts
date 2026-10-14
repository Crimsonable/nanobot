import type { PluginInput } from "@opencode-ai/plugin"

type Settings = {
  enabled: boolean; text: boolean; reasoning: boolean; tools: boolean
  tool_arguments: boolean; tool_output: boolean; max_chars: number
}
type Context = {
  schema_version: number; frontend_id: string; user_id: string; identity_id: string
  bucket_id: string; chat_id?: string; input_message_id?: string
  requests: Record<string, string>; parent_session_id?: string
}
const defaults: Settings = { enabled: true, text: true, reasoning: true, tools: true,
  tool_arguments: true, tool_output: true, max_chars: 16000 }
const secretKey = /^(?:_runtime_ticket|authorization|cookie|api[-_]?key|access[-_]?key|secret[-_]?key|password|token|access_token|refresh_token|client_secret)$/i

// Restrict retained state; all cache misses can be resolved from OpenCode metadata/messages.
function remember<T>(cache: Map<string, T>, key: string, value: T) {
  cache.delete(key)
  cache.set(key, value)
  if (cache.size > 4096) cache.delete(cache.keys().next().value!)
}

export function createSessionLogger(client: PluginInput["client"], options: Partial<Settings> = {},
  write: (line: string) => void = line => { process.stdout.write(line + "\n") }) {
  const settings = { ...defaults, ...options }
  const contexts = new Map<string, Context>()
  const messages = new Map<string, { role: string; parentID?: string }>()
  const parts = new Map<string, { type: string; length: number; tail: string }>()
  const calls = new Map<string, { context: Context; request_id?: string }>()
  const terminalCalls = new Map<string, string>()
  let pending = Promise.resolve()

  function schedule(action: () => Promise<void>) {
    if (!settings.enabled) return Promise.resolve()
    pending = pending.then(action).catch(error => {
      // Logging must not fail a model/tool call; report failures without dumping credentials.
      try { write(JSON.stringify({ timestamp: new Date().toISOString(), logger: "session",
        event: "logging.error", error: error?.name ?? "Error" })) } catch { /* stdout unavailable */ }
    })
    return pending
  }

  function managed(info: any): Context | undefined {
    const value = info?.metadata?.bucket_runtime
    if (value?.schema_version !== 1 || !["frontend_id", "user_id", "identity_id", "bucket_id"]
      .every(key => typeof value[key] === "string" && value[key])) return
    return { ...value, requests: { ...(value.requests ?? {}) } }
  }

  async function context(session_id: string, fresh = false, seen = new Set<string>()): Promise<Context | undefined> {
    if (!fresh && contexts.has(session_id)) return contexts.get(session_id)
    if (seen.has(session_id) || seen.size >= 64 || !client?.session) return
    seen.add(session_id)
    const response = await client.session.get({ path: { id: session_id }, signal: AbortSignal.timeout(2000) })
    if (response.error) throw new Error("session lookup failed")
    const info: any = response.data
    let value = managed(info)
    if (!value && info?.parentID) {
      const parent = await context(info.parentID, false, seen)
      // Child sessions inherit the owner, never the parent's mutable current request.
      if (parent) value = { ...parent, requests: {}, input_message_id: undefined, parent_session_id: info.parentID }
    }
    if (value) remember(contexts, session_id, value)
    return value
  }

  async function message(session_id: string, message_id: string) {
    const key = session_id + ":" + message_id
    if (messages.has(key)) return messages.get(key)
    const response = await client.session.message({ path: { id: session_id, messageID: message_id },
      signal: AbortSignal.timeout(2000) })
    if (response.error) throw new Error("message lookup failed")
    const info = response.data?.info
    if (info) remember(messages, key, info)
    return info
  }

  function clean(value: unknown): string {
    const text = typeof value === "string" ? value : JSON.stringify(value, (key, item) => secretKey.test(key) ? "[REDACTED]" : item)
    return (text ?? "").replace(/\bBearer\s+[^\s"']+/gi, "Bearer [REDACTED]")
  }

  function emit(ctx: Context, session_id: string, event: string, fields: Record<string, unknown>) {
    const record: Record<string, unknown> = { timestamp: new Date().toISOString(), logger: "session", event,
      frontend_id: ctx.frontend_id, user_id: ctx.user_id, identity_id: ctx.identity_id,
      bucket_id: ctx.bucket_id, chat_id: ctx.chat_id, session_id,
      ...(ctx.parent_session_id ? { parent_session_id: ctx.parent_session_id } : {}), ...fields }
    for (const key of ["text", "arguments", "output", "error"]) {
      if (record[key] === undefined) continue
      const value = clean(record[key])
      record[key] = value.slice(0, settings.max_chars)
      if (value.length > settings.max_chars) record[key + "_truncated"] = true
    }
    write(JSON.stringify(record))
  }

  async function onEvent(event: any) {
    const properties = event.properties ?? {}
    const info = properties.info
    const session_id = properties.sessionID ?? properties.part?.sessionID ?? info?.sessionID ??
      (event.type.startsWith("session.") ? info?.id : undefined)
    if (!session_id) return
    if (event.type === "session.created" || event.type === "session.updated") {
      const value = managed(info)
      if (value) remember(contexts, session_id, value)
      return
    }
    if (event.type === "session.deleted") {
      contexts.delete(session_id)
      return
    }
    if (event.type === "message.updated") {
      if (info?.id) remember(messages, session_id + ":" + info.id, info)
      return
    }
    if (event.type !== "message.part.updated" && event.type !== "message.part.delta" && event.type !== "session.error") return
    let ctx = await context(session_id)
    if (!ctx) return // Only sessions created by our runtime carry managed identity.
    if (event.type === "session.error") {
      emit(ctx, session_id, "session.error", { error: properties.error })
      return
    }
    const part = properties.part
    const message_id = part?.messageID ?? properties.messageID
    const part_id = part?.id ?? properties.partID
    if (!message_id || !part_id) return
    const messageInfo = await message(session_id, message_id)
    if (messageInfo?.role !== "assistant") return
    const input_message_id = messageInfo.parentID
    if (input_message_id && !ctx.requests[input_message_id] && !ctx.parent_session_id) {
      ctx = await context(session_id, true) ?? ctx
    }
    const request_id = input_message_id ? ctx.requests[input_message_id] : undefined
    const fields = { request_id, message_id, part_id }
    if (part?.type === "tool") {
      if (!settings.tools) return
      const status = part.state?.status
      if (status !== "completed" && status !== "error") return
      const key = session_id + ":" + part.callID
      if (terminalCalls.get(key) === status) return
      remember(terminalCalls, key, status)
      const call = calls.get(key)
      emit(call?.context ?? ctx, session_id, status === "error" ? "tool.error" : "tool.end", {
        ...fields, request_id: call?.request_id ?? request_id, call_id: part.callID, tool: part.tool, status,
        ...(status === "error" ? { error: part.state.error } : {}),
        ...(settings.tool_output && status === "completed" ? { output: part.state.output } : {}) })
      calls.delete(key)
      return
    }
    const key = session_id + ":" + message_id + ":" + part_id
    const previous = parts.get(key)
    const type = part?.type ?? previous?.type
    if (type !== "text" && type !== "reasoning") return
    let delta: string
    let reset = false
    let length: number
    let tail: string
    if (event.type === "message.part.delta") {
      if (properties.field !== "text" || typeof properties.delta !== "string") return
      delta = properties.delta
      length = (previous?.length ?? 0) + delta.length
      tail = ((previous?.tail ?? "") + delta).slice(-256)
    } else {
      const text = part.text ?? ""
      reset = !!previous && (text.length < previous.length ||
        text.slice(Math.max(0, previous.length - 256), previous.length) !== previous.tail)
      delta = reset ? text : text.slice(previous?.length ?? 0)
      length = text.length
      tail = text.slice(-256)
    }
    remember(parts, key, { type, length, tail })
    if (delta && settings[type]) emit(ctx, session_id, type, { ...fields, text: delta, ...(reset ? { reset } : {}) })
  }

  return {
    event: (event: any) => schedule(() => onEvent(event)),
    toolStart: (input: { sessionID: string; callID: string; tool: string }, args: unknown) => schedule(async () => {
      if (!settings.tools) return
      const ctx = await context(input.sessionID, true)
      if (!ctx) return
      const request_id = ctx.input_message_id ? ctx.requests[ctx.input_message_id] : undefined
      remember(calls, input.sessionID + ":" + input.callID, { context: ctx, request_id })
      emit(ctx, input.sessionID, "tool.start", { request_id, call_id: input.callID, tool: input.tool,
        ...(settings.tool_arguments ? { arguments: args } : {}) })
    }),
    flush: () => pending,
  }
}
