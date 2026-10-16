import { randomUUID } from "node:crypto"

type Envelope = { type: string; event_id: string; directory: string; event: any }

// Direct delivery only: no attribution, queue, retries or session logging here.
export function createRuntimeEvents(directory: string, send: (body: Envelope) => Promise<unknown>) {
  return {
    event(event: any) {
      return send({ type: "opencode.event", event_id: "oc_" + randomUUID(), directory,
        event: structuredClone(event) })
    },
  }
}
