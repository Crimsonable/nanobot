import assert from "node:assert/strict"
import { test } from "node:test"
import { BucketRuntimePlugin } from "./dist/index.mjs"
import { createSessionLogger } from "./dist/session-logger.mjs"
import { createRuntimeEvents } from "./dist/runtime-events.mjs"

function logFixture(options = {}) {
  const rows = []
  const sessions = new Map()
  const messages = new Map()
  const client = { session: {
    get: async ({ path }) => ({ data: structuredClone(sessions.get(path.id)) }),
    message: async ({ path }) => ({ data: { info: messages.get(path.messageID) } }),
  } }
  const logger = createSessionLogger(client, options, line => rows.push(JSON.parse(line)))
  const owner = (user_id, request_id) => ({ schema_version: 1, user_id, frontend_id: "web",
    identity_id: "identity-" + user_id, bucket_id: "bucket", chat_id: "group-1",
    input_message_id: "input-" + request_id, requests: { ["input-" + request_id]: request_id } })
  const register = async (session_id, context) => {
    const info = { id: session_id, metadata: { bucket_runtime: context } }
    sessions.set(session_id, info)
    await logger.event({ type: "session.updated", properties: { info } })
  }
  const part = (sessionID, messageID, id, type, text) => ({ type: "message.part.updated",
    properties: { part: { sessionID, messageID, id, type, text } } })
  return { logger, rows, sessions, messages, owner, register, part }
}

test("metadata separates concurrent users and retains request attribution on session reuse", async () => {
  const f = logFixture()
  await f.register("alice-session", f.owner("alice", "r1"))
  await f.register("bob-session", f.owner("bob", "r2"))
  f.messages.set("answer-a", { role: "assistant", parentID: "input-r1" })
  f.messages.set("answer-b", { role: "assistant", parentID: "input-r2" })
  await Promise.all([
    f.logger.event(f.part("alice-session", "answer-a", "pa", "reasoning", "Planning")),
    f.logger.event(f.part("bob-session", "answer-b", "pb", "text", "Bob reply")),
  ])
  const updated = f.owner("alice", "r3")
  updated.requests["input-r1"] = "r1"
  await f.register("alice-session", updated)
  await f.logger.event({ type: "message.part.delta", properties: { sessionID: "alice-session",
    messageID: "answer-a", partID: "pa", field: "text", delta: " done" } })
  await f.logger.event(f.part("alice-session", "answer-a", "pa", "reasoning", "Planning done"))
  assert.deepEqual(f.rows.map(r => [r.user_id, r.request_id, r.text]), [
    ["alice", "r1", "Planning"], ["bob", "r2", "Bob reply"], ["alice", "r1", " done"],
  ])
  assert.ok(f.rows.every(r => r.chat_id === "group-1" && !("conversation_id" in r)))
})

test("tools record original arguments, redact credentials, truncate output and include failures", async () => {
  const f = logFixture({ max_chars: 256 })
  await f.register("s", f.owner("alice", "r1"))
  f.messages.set("a", { role: "assistant", parentID: "input-r1" })
  await f.logger.toolStart({ sessionID: "s", callID: "c1", tool: "bash" }, {
    command: "echo original", api_key: "secret", _runtime_ticket: "ticket",
  })
  const tool = { type: "message.part.updated", properties: { part: { sessionID: "s", messageID: "a",
    id: "p1", type: "tool", callID: "c1", tool: "bash", state: { status: "completed", output: "x".repeat(400) } } } }
  await f.logger.event(tool)
  await f.logger.event(tool)
  await f.logger.event({ ...tool, properties: { part: { ...tool.properties.part, callID: "c2", id: "p2",
    state: { status: "error", error: "denied" } } } })
  assert.equal(f.rows[0].arguments, '{"command":"echo original","api_key":"[REDACTED]","_runtime_ticket":"[REDACTED]"}')
  assert.equal(f.rows[1].output.length, 256)
  assert.equal(f.rows[1].output_truncated, true)
  assert.equal(f.rows[2].event, "tool.error")
  assert.equal(f.rows[2].request_id, "r1")
  assert.equal(f.rows.length, 3)
})

test("child sessions resolve the owner through parents without borrowing a later request", async () => {
  const f = logFixture()
  await f.register("parent", f.owner("alice", "r2"))
  f.sessions.set("child", { id: "child", parentID: "parent" })
  f.messages.set("a", { role: "assistant", parentID: "child-input" })
  await f.logger.event(f.part("child", "a", "p", "text", "child reply"))
  assert.equal(f.rows[0].user_id, "alice")
  assert.equal(f.rows[0].parent_session_id, "parent")
  assert.equal(f.rows[0].request_id, undefined)
  f.sessions.set("unmanaged", { id: "unmanaged" })
  await f.logger.event(f.part("unmanaged", "a", "p", "text", "ignore"))
  assert.equal(f.rows.length, 1)
})

test("frontend logging switches suppress content and lookup failures do not fail hooks", async () => {
  const f = logFixture({ reasoning: false, tools: false })
  await f.register("s", f.owner("alice", "r1"))
  f.messages.set("a", { role: "assistant", parentID: "input-r1" })
  await f.logger.event(f.part("s", "a", "p", "reasoning", "hidden"))
  await f.logger.toolStart({ sessionID: "s", callID: "c1", tool: "bash" }, {})
  assert.equal(f.rows.length, 0)
  const lines = []
  const broken = createSessionLogger({ session: { get: async () => { throw new Error("secret") } } }, {}, line => lines.push(line))
  await broken.toolStart({ sessionID: "s", callID: "c", tool: "bash" }, {})
  assert.equal(JSON.parse(lines[0]).event, "logging.error")
  assert.ok(!lines[0].includes("secret"))
  const disabled = createSessionLogger({}, { enabled: false }, () => assert.fail("logging disabled"))
  await disabled.toolStart({ sessionID: "s", callID: "c", tool: "bash" }, {})
})

test("hook replaces commands and publication tickets using trusted runtime identity", async () => {
  const savedFetch = globalThis.fetch
  const savedEnv = { ...process.env }
  const calls = []
  Object.assign(process.env, {
    BUCKET_RPC_URL: "http://127.0.0.1:4097", BUCKET_RPC_TOKEN: "test-token",
    BUCKET_EXEC_PYTHON: "/usr/bin/python3", BUCKET_EXEC_CLIENT: "/opt/app/exec_client.py",
  })
  globalThis.fetch = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body), headers: init.headers })
    return { ok: true, json: async () => ({ ticket: "trusted-ticket", host_workdir: "/opt/context" }) }
  }
  try {
    const hooks = await BucketRuntimePlugin({ directory: "/workspace/alice" })
    const output = { args: { command: "echo $(cat /secret)", workdir: "/other-user" } }
    await hooks["tool.execute.before"]({ tool: "bash", sessionID: "session-a", callID: "call-a" }, output)
    assert.equal(output.args.command, "'/usr/bin/python3' '/opt/app/exec_client.py' 'trusted-ticket'")
    assert.equal(output.args.workdir, "/opt/context")
    const authorization = calls.find(c => c.url.endsWith("/authorize"))
    assert.equal(authorization.body.session_id, "session-a")
    assert.equal(authorization.body.directory, "/workspace/alice")
    assert.equal(authorization.body.args.command, "echo $(cat /secret)")
    assert.equal(calls[1].body.event.type, "tool.execute.before")
    assert.equal(calls[1].body.event.properties.args.command, "echo $(cat /secret)")
    const publish = { args: { files: [{ path: "output/report.pdf" }], _runtime_ticket: "forged" } }
    await hooks["tool.execute.before"]({ tool: "publish_attachment", sessionID: "session-a", callID: "call-b" }, publish)
    assert.equal(publish.args._runtime_ticket, "trusted-ticket")
    assert.equal(calls.filter(c => c.url.endsWith("/authorize"))[1].body.args._runtime_ticket, undefined)
    const native = { args: { filePath: "/workspace/file.txt" } }
    await hooks["tool.execute.before"]({ tool: "read", sessionID: "session-a", callID: "call-c" }, native)
    assert.deepEqual(native.args, { filePath: "/workspace/file.txt" })
    assert.equal(calls.length, 7)
    assert.equal(hooks["permission.ask"], undefined)
    assert.equal(hooks["command.execute.before"], undefined)
  } finally {
    globalThis.fetch = savedFetch
    for (const key of Object.keys(process.env)) if (!(key in savedEnv)) delete process.env[key]
    Object.assign(process.env, savedEnv)
  }
})

function eventFixture() {
  const sent = [], errors = []
  const relay = createRuntimeEvents({ session: { get: async () => assert.fail("unexpected lookup") } },
    "/users/alice", async body => { sent.push(structuredClone(body)) }, error => errors.push(error))
  const context = async (input, request) => relay.event({ type: "session.updated", properties: { info: {
    id: "s", metadata: { bucket_runtime: { schema_version: 1, requests: { [input]: request } } } } } })
  const message = async (id, parentID, finish, completed) => relay.event({ type: "message.updated", properties: {
    info: { id, sessionID: "s", role: "assistant", parentID, finish, time: { created: id === "a1" ? 1 : 2, completed },
      tokens: { input: 10, output: 5 } } } })
  const part = async (messageID, id, type, fields) => relay.event({ type: "message.part.updated", properties: {
    part: { sessionID: "s", messageID, id, type, ...fields } } })
  return { relay, sent, errors, context, message, part }
}

test("plugin pushes a correlated final snapshot, never finishes on tools/idle or includes reasoning", async () => {
  const f = eventFixture()
  await f.context("input-1", "r1")
  await f.message("a1", "input-1")
  await f.part("a1", "tool", "tool", { callID: "c1", tool: "bash", state: { status: "running" } })
  await f.message("a1", "input-1", "tool-calls", 1)
  await f.relay.event({ type: "session.idle", properties: { sessionID: "s" } })
  await f.message("a2", "input-1")
  await f.part("a2", "reason", "reasoning", { text: "private reasoning" })
  await f.part("a2", "text", "text", { text: "Hello" })
  await f.relay.event({ type: "message.part.delta", properties: { sessionID: "s", messageID: "a2",
    partID: "text", field: "text", delta: " world" } })
  await f.part("a2", "text", "text", { text: "Hello world" })
  await f.message("a2", "input-1", "stop", 2)
  assert.equal(f.sent.filter(x => x.type === "result").length, 0, "a pending tool prevents completion")
  await f.part("a1", "tool", "tool", { callID: "c1", tool: "bash", state: { status: "completed" } })
  const results = f.sent.filter(x => x.type === "result")
  assert.equal(results.length, 1)
  assert.equal(results[0].messages[1].parts[0].text, "Hello world")
  assert.equal(results[0].input_message_id, "input-1")
  assert.equal(results[0].request_id, "r1")
  assert.equal(results[0].directory, "/users/alice")
  assert.ok(!JSON.stringify(results).includes("private reasoning"))
  await f.message("a2", "input-1", "stop", 2)
  assert.equal(f.sent.filter(x => x.type === "result").length, 1)
  await f.context("input-2", "r2")
  await f.message("a3", "input-2")
  await f.part("a3", "text2", "text", { text: "Second reply" })
  await f.message("a2", "input-1", "stop", 2) // Old completion cannot finish the reused session's new request.
  await f.message("a3", "input-2", "stop", 3)
  assert.equal(f.sent.at(-1).request_id, "r2")
  assert.equal(f.sent.at(-1).messages.length, 1)
  assert.deepEqual(f.errors, [])
})

test("plugin retains early text, reports errors and retries with the same event identity", async () => {
  const f = eventFixture()
  await f.context("input-1", "r1")
  await f.part("a1", "text", "text", { text: "arrived before message info" })
  await f.message("a1", "input-1", "stop", 1)
  assert.equal(f.sent.at(-1).messages[0].parts[0].text, "arrived before message info")
  await f.context("input-2", "r2")
  await f.message("a2", "input-2")
  await f.relay.event({ type: "session.error", properties: { sessionID: "s", error: { name: "APIError" } } })
  assert.equal(f.sent.at(-1).type, "error")
  assert.equal(f.sent.at(-1).request_id, "r2")
  const attempts = []
  const relay = createRuntimeEvents({}, "/users/bob", async body => {
    attempts.push(structuredClone(body))
    if (attempts.length === 1) throw new Error("temporary failure")
  })
  await relay.event({ type: "session.updated", properties: { info: { id: "b", metadata: {
    bucket_runtime: { schema_version: 1, requests: { input: "req" } } } } } })
  await relay.event({ type: "message.updated", properties: { info: { id: "input", role: "user", sessionID: "b" } } })
  await relay.event({ type: "session.error", properties: { sessionID: "b", error: { name: "APIError" } } })
  assert.equal(attempts.length, 5) // Three raw events, the final error, and one raw retry.
  assert.deepEqual(attempts[0], attempts[1])
  assert.equal(attempts[0].type, "opencode.event")
})

test("disabling plugin logs does not disable final replies", async () => {
  const savedFetch = globalThis.fetch, savedEnv = { ...process.env }
  const calls = []
  Object.assign(process.env, { BUCKET_RPC_URL: "http://127.0.0.1:4097", BUCKET_RPC_TOKEN: "token",
    BUCKET_EXEC_PYTHON: "/usr/bin/python3", BUCKET_EXEC_CLIENT: "/exec.py", BUCKET_SESSION_LOGGING: '{"enabled":false}' })
  globalThis.fetch = async (url, init) => { calls.push({ url, body: JSON.parse(init.body) }); return { ok: true, json: async () => ({ ok: true }) } }
  try {
    const hooks = await BucketRuntimePlugin({ directory: "/users/alice" })
    await hooks.event({ event: { type: "session.updated", properties: { info: { id: "s", metadata: {
      bucket_runtime: { schema_version: 1, requests: { input: "r" } } } } } } })
    await hooks.event({ event: { type: "message.updated", properties: { info: {
      id: "a", parentID: "input", sessionID: "s", role: "assistant" } } } })
    await hooks.event({ event: { type: "message.part.updated", properties: { part: {
      id: "p", messageID: "a", sessionID: "s", type: "text", text: "done" } } } })
    await hooks.event({ event: { type: "message.updated", properties: { info: {
      id: "a", parentID: "input", sessionID: "s", role: "assistant", finish: "stop", time: { completed: 1 } } } } })
    await hooks.dispose()
    assert.equal(calls[0].body.version, "3")
    assert.equal(calls.at(-1).url, "http://127.0.0.1:4097/plugin/event")
    assert.equal(calls.at(-1).body.type, "result")
  } finally {
    globalThis.fetch = savedFetch
    for (const key of Object.keys(process.env)) if (!(key in savedEnv)) delete process.env[key]
    Object.assign(process.env, savedEnv)
  }
})

test("compaction summaries cannot complete a user request", async () => {
  const f = eventFixture()
  await f.context("input-1", "r1")
  await f.relay.event({ type: "message.updated", properties: { info: { id: "summary", sessionID: "s",
    parentID: "input-1", role: "assistant", summary: true, finish: "stop", time: { completed: 1 } } } })
  await f.part("summary", "p", "text", { text: "internal summary" })
  assert.ok(!f.sent.some(e => e.type === "result"))
  assert.ok(!f.sent.some(e => e.content?.includes("internal summary")))
})

test("all raw events reach runtime, including reasoning, unknown, child and post-final events", async () => {
  const f = eventFixture()
  await f.context("input-1", "r1")
  await f.message("a1", "input-1")
  const native = [
    { type: "message.part.updated", properties: { part: {
      id: "reason", messageID: "a1", sessionID: "s", type: "reasoning", text: "思考" } } },
    { type: "message.part.delta", properties: { sessionID: "s", messageID: "a1",
      partID: "reason", field: "text", delta: "连续内容" } },
    { type: "future.event", properties: { content: "x".repeat(240001), extra: { preserved: true } } },
    { type: "session.created", properties: { info: { id: "child", parentID: "s" } } },
    { type: "file.edited", properties: { file: "/users/alice/a.txt" } },
  ]
  for (const event of native) await f.relay.event(event)
  const raw = f.sent.filter(e => e.type === "opencode.event").slice(2)
  assert.deepEqual(raw.map(e => e.event), native)
  assert.equal(raw[0].request_id, "r1")
  assert.equal(raw[1].input_message_id, "input-1")
  assert.equal(raw[2].request_id, "")
  assert.equal(raw[3].session_id, "child")
  assert.equal(raw[3].request_id, "")
  assert.equal(new Set(raw.map(e => e.event_id)).size, native.length)
  await f.part("a1", "text", "text", { text: "final" })
  await f.message("a1", "input-1", "stop", 2)
  await f.context("input-2", "r2")
  await f.message("a2", "input-2")
  const late = { type: "message.part.delta", properties: { sessionID: "s", messageID: "a1",
    partID: "reason", field: "text", delta: "late" } }
  await f.relay.event(late)
  assert.deepEqual(f.sent.at(-1).event, late)
  assert.equal(f.sent.at(-1).input_message_id, "input-1")
  assert.notEqual(f.sent.at(-1).request_id, "r2")
  assert.equal(f.sent.filter(e => e.type === "result").length, 1)
  assert.deepEqual(f.errors, [])
})
