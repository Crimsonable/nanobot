import type { PluginInput } from "@opencode-ai/plugin"
import { randomUUID } from "node:crypto"

type Turn = { input: string; request: string; messages: Map<string, any>; revision: number; lastProgress: number }

// Replies come exclusively from plugin events. No message-list fetches or timers
// querying OpenCode: metadata lookups only identify a managed session on cache miss.
export function createRuntimeEvents(client: PluginInput["client"], directory: string,
  send: (body: any) => Promise<unknown>, report: (error: unknown) => void = () => {}) {
  const contexts = new Map<string, any>()
  const turns = new Map<string, Turn>()
  const closed = new Set<string>()
  const earlyParts = new Map<string, Map<string, any>>()
  const inputs = new Map<string, string>()
  const toolMessages = new Map<string, string>()
  let pending = Promise.resolve()

  function close(session: string, turn: Turn) {
    closed.add(session + ":" + turn.input)
    if (closed.size > 4096) closed.delete(closed.values().next().value!)
    turns.delete(session)
  }

  async function context(session: string, input: string) {
    let ctx = contexts.get(session)
    if (!ctx?.requests?.[input]) {
      const response = await client.session.get({ path: { id: session }, signal: AbortSignal.timeout(2000) })
      if (response.error) throw new Error("session metadata lookup failed")
      const info: any = response.data
      ctx = info?.metadata?.bucket_runtime
      if (ctx?.schema_version === 1) {
        contexts.set(session, ctx)
        if (contexts.size > 4096) contexts.delete(contexts.keys().next().value!)
      }
    }
    return ctx?.schema_version === 1 ? ctx : undefined
  }

  async function sendWithRetry(body: any) {
    for (let attempt = 0; ; attempt++) {
      try { await send(body); return } catch (error) {
        if (attempt === 2) throw error
        await new Promise(resolve => setTimeout(resolve, 50 * 2 ** attempt))
      }
    }
  }

  async function deliver(session: string, turn: Turn, fields: any) {
    await sendWithRetry({ session_id: session, request_id: turn.request, input_message_id: turn.input,
      directory, revision: ++turn.revision, ...fields })
  }

  async function forward(event: any, eventID: string) {
    const p = event.properties ?? {}, info = p.info
    const session = p.sessionID ?? p.part?.sessionID ?? info?.sessionID ??
      (event.type.startsWith("session.") ? info?.id : undefined) ?? ""
    if (["session.created", "session.updated"].includes(event.type)) {
      const ctx = info?.metadata?.bucket_runtime
      if (ctx?.schema_version === 1) contexts.set(session, ctx)
    }
    const message = p.messageID ?? p.part?.messageID ??
      (event.type === "message.updated" ? info?.id : undefined) ?? toolMessages.get(session + ":" + p.callID) ?? ""
    if (p.part?.type === "tool" && p.part.callID && message) {
      toolMessages.set(session + ":" + p.part.callID, message)
      if (toolMessages.size > 8192) toolMessages.delete(toolMessages.keys().next().value!)
    }
    const key = session + ":" + message
    if (event.type === "message.updated") {
      const input = info?.role === "user" ? info.id : info?.parentID
      if (input) inputs.set(key, input)
      if (inputs.size > 8192) inputs.delete(inputs.keys().next().value!)
    }
    const input = inputs.get(key) ?? ""
    // Session/global/child events without a known parent stay uncorrelated. Never
    // assign a delayed event to whichever request happens to be running now.
    await sendWithRetry({ type: "opencode.event", event_id: eventID, directory,
      session_id: session, message_id: message, input_message_id: input,
      request_id: contexts.get(session)?.requests?.[input] ?? "", event })
  }

  function records(turn: Turn) {
    return [...turn.messages.values()].map(message => ({ info: message.info, parts: [...message.parts.values()] }))
  }

  async function complete(session: string, turn: Turn) {
    const messages = records(turn)
    if (messages.some(m => m.parts.some((p: any) => p.type === "tool" && ["pending", "running"].includes(p.state?.status)))) return
    const last = messages.filter(m => !m.info.summary).sort((a, b) => (a.info.time?.created ?? 0) - (b.info.time?.created ?? 0)
      || a.info.id.localeCompare(b.info.id)).at(-1)
    if (!last?.info.time?.completed) return
    if (!last.info.error && !["stop", "end_turn", "length", "content-filter"].includes(last.info.finish)) return
    await deliver(session, turn, { type: "result", messages })
    close(session, turn)
  }

  async function onEvent(event: any) {
    const p = event.properties ?? {}
    const info = p.info
    const session = p.sessionID ?? p.part?.sessionID ?? info?.sessionID ??
      (event.type.startsWith("session.") ? info?.id : undefined)
    if (!session) return
    if (["session.created", "session.updated"].includes(event.type)) {
      const ctx = info?.metadata?.bucket_runtime
      if (ctx?.schema_version === 1) {
        contexts.set(session, ctx)
        if (contexts.size > 4096) contexts.delete(contexts.keys().next().value!)
      }
      return
    }
    if (event.type === "session.deleted") { contexts.delete(session); turns.delete(session); return }
    if (event.type === "message.updated") {
      const input = info.role === "user" ? info.id : info.parentID
      if (!input || closed.has(session + ":" + input)) return
      const ctx = await context(session, input)
      if (!ctx?.requests?.[input]) return // Child/unmanaged messages cannot finish a root request.
      let turn = turns.get(session)
      if (!turn) {
        turn = { input, request: ctx.requests[input], messages: new Map(), revision: 0, lastProgress: 0 }
        turns.set(session, turn)
      }
      if (turn.input !== input || info.role !== "assistant") return
      const key = session + ":" + info.id
      const parts = turn.messages.get(info.id)?.parts ?? earlyParts.get(key) ?? new Map()
      earlyParts.delete(key)
      turn.messages.set(info.id, { info: { id: info.id, sessionID: session, role: info.role,
        parentID: info.parentID, time: info.time, finish: info.finish, error: info.error,
        summary: info.summary, tokens: info.tokens }, parts })
      await complete(session, turn)
      return
    }
    const turn = turns.get(session)
    if (event.type === "session.error") {
      if (turn) {
        await deliver(session, turn, { type: "error", error: p.error?.name ?? "MODEL_ERROR" })
        close(session, turn)
      }
      return
    }
    if (!["message.part.updated", "message.part.delta"].includes(event.type)) return
    const part = p.part
    const messageID = part?.messageID ?? p.messageID
    const partID = part?.id ?? p.partID
    if (!messageID || !partID) return
    const message = turn?.messages.get(messageID)
    const key = session + ":" + messageID
    const parts: Map<string, any> = message?.parts ?? earlyParts.get(key) ?? new Map()
    if (part?.type === "text") parts.set(partID, { id: partID, type: "text", text: part.text ?? "", ignored: part.ignored })
    else if (part?.type === "tool") parts.set(partID, { id: partID, type: "tool", callID: part.callID,
      tool: part.tool, state: { status: part.state?.status } })
    else if (event.type === "message.part.delta" && p.field === "text" && parts.get(partID)?.type === "text") {
      parts.get(partID).text += p.delta
    } else return // All parts are forwarded above; only reply text enters the final result.
    if (!message || !turn) {
      earlyParts.set(key, parts)
      if (earlyParts.size > 256) earlyParts.delete(earlyParts.keys().next().value!)
      return
    }
    if (part?.type === "tool") await deliver(session, turn, { type: "tool.progress",
      call_id: part.callID, tool: part.tool, status: part.state?.status })
    else if (Date.now() - turn.lastProgress >= 100) {
      turn.lastProgress = Date.now()
      const content = records(turn).filter(m => !m.info.summary).flatMap(m => m.parts)
        .filter((v: any) => v.type === "text" && !v.ignored && v.text).map((v: any) => v.text).join("\n").slice(-200000)
      await deliver(session, turn, { type: "response.delta", content })
    }
    await complete(session, turn)
  }

  return {
    event(event: any) {
      const snapshot = structuredClone(event)
      const eventID = "oc_" + randomUUID()
      pending = pending.then(async () => {
        // No type whitelist, throttling, content truncation or terminal-state filter.
        await forward(snapshot, eventID)
        await onEvent(snapshot)
      }).catch(report)
      return pending
    },
    flush: () => pending,
  }
}
