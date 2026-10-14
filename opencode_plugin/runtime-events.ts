import { randomUUID, createHash } from "node:crypto"
import { mkdir, readdir, readFile, rename, unlink, open } from "node:fs/promises"
import { join } from "node:path"

type Envelope = { type: string; event_id: string; directory: string; session_id: string;
  message_id: string; input_message_id: string; request_id: string; event: any }

// One native event stream. Persist before returning to OpenCode; remove only after runtime ACK.
export async function createRuntimeEvents(directory: string, root: string,
  send: (body: Envelope) => Promise<unknown>, log: (body: Envelope, owner: any) => void) {
  const folder = join(root, createHash("sha256").update(directory).digest("hex"))
  await mkdir(folder, { recursive: true })
  const entries = await readdir(folder)
  // An unfinished write was never acknowledged to OpenCode. It is not a queued event.
  for (const name of entries.filter(name => /^\d{20}\.json\.tmp$/.test(name))) await unlink(join(folder, name))
  const files = entries.filter(name => /^\d{20}\.json$/.test(name)).sort()
  let sequence = files.length ? Number(files.at(-1)!.split(".")[0]) : 0
  const contexts = new Map<string, any>()
  const inputs = new Map<string, string>()
  const activeInputs = new Map<string, string>()
  const tools = new Map<string, string>()
  const parts = new Map<string, string>()
  let pending = Promise.resolve()
  let worker: Promise<void> | undefined
  let failure: unknown

  async function drain() {
    while (files.length) {
      const file = join(folder, files[0])
      const body = JSON.parse(await readFile(file, "utf8"))
      try {
        await send(body)
      } catch (error) {
        console.error("Runtime event unacknowledged; retained for delivery", body.event_id)
        await new Promise(resolve => setTimeout(resolve, 1000))
        continue
      }
      await unlink(file)
      files.shift()
    }
  }
  function start() {
    if (worker || failure) return
    worker = drain().catch(error => { failure = error }).finally(() => {
      worker = undefined
      if (files.length && !failure) start()
    })
  }
  start()

  return {
    event(event: any) {
      const snapshot = structuredClone(event)
      pending = pending.then(async () => {
        if (failure) throw failure
        const p = snapshot.properties ?? {}, info = p.info
        const session = p.sessionID ?? p.part?.sessionID ?? info?.sessionID ??
          (snapshot.type.startsWith("session.") ? info?.id : undefined) ?? ""
        if (["session.created", "session.updated"].includes(snapshot.type)) {
          const ctx = info?.metadata?.bucket_runtime
          if (ctx?.schema_version === 1) contexts.set(session, ctx)
          else if (info?.parentID && contexts.has(info.parentID)) {
            contexts.set(session, { ...contexts.get(info.parentID), requests: {}, parent_session_id: info.parentID })
          }
        }
        const message = p.messageID ?? p.part?.messageID ??
          (snapshot.type === "message.updated" ? info?.id : undefined) ?? tools.get(session + ":" + p.callID) ?? ""
        if (p.part?.callID) tools.set(session + ":" + p.part.callID, message)
        if (p.part?.type) parts.set(session + ":" + p.part.id, p.part.type)
        if (snapshot.type === "message.updated") {
          inputs.set(session + ":" + message, info.role === "user" ? info.id : info.parentID)
          if (info.role === "user") activeInputs.set(session, info.id)
        }
        const input = (snapshot.type === "session.error" ? activeInputs.get(session) : inputs.get(session + ":" + message)) ?? ""
        const owner = contexts.get(session)
        const body: Envelope = { type: "opencode.event", event_id: "oc_" + randomUUID(), directory,
          session_id: session, message_id: message, input_message_id: input,
          request_id: owner?.requests?.[input] ?? "", event: snapshot }
        const name = String(++sequence).padStart(20, "0") + ".json"
        const target = join(folder, name)
        const file = await open(target + ".tmp", "wx")
        try { await file.writeFile(JSON.stringify(body)); await file.sync() } finally { await file.close() }
        await rename(target + ".tmp", target)
        files.push(name)
        start()
        log(body, { ...owner, part_type: p.part?.type ?? parts.get(session + ":" + p.partID) })
        if (snapshot.type === "session.idle") activeInputs.delete(session)
        if (snapshot.type === "session.deleted") {
          contexts.delete(session)
          activeInputs.delete(session)
          for (const map of [inputs, tools, parts]) {
            for (const key of map.keys()) if (key.startsWith(session + ":")) map.delete(key)
          }
        }
      })
      return pending
    },
    async flush() {
      await pending
      while (worker) await worker
      if (failure) throw failure
    },
  }
}
