import assert from "node:assert/strict"
import { test } from "node:test"
import { mkdtemp, mkdir, readdir, writeFile, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { createHash } from "node:crypto"
import { createRuntimeEvents } from "./dist/runtime-events.mjs"
import { createSessionLogger } from "./dist/session-logger.mjs"
import { BucketRuntimePlugin } from "./dist/index.mjs"

const settings = { enabled: true, text: true, reasoning: true, tools: true,
  tool_arguments: true, tool_output: true, max_chars: 16000 }
const owner = { schema_version: 1, frontend_id: "web", user_id: "alice", bucket_id: "b",
  chat_id: "chat", input_message_id: "input", requests: { input: "request" } }
const session = { type: "session.updated", properties: { info: { id: "s", metadata: { bucket_runtime: owner } } } }

test("one attributed native stream includes reasoning, tools and unknown events without API reads", async () => {
  const root = await mkdtemp(join(tmpdir(), "events-")), sent = [], logged = []
  try {
    const events = await createRuntimeEvents("/alice", root, async body => sent.push(body),
      createSessionLogger(settings, line => logged.push(JSON.parse(line))))
    await events.event(session)
    await events.event({type:"message.updated", properties:{info:{id:"answer", sessionID:"s", role:"assistant", parentID:"input"}}})
    const reasoning = { type:"message.part.updated", properties:{part:{id:"p", messageID:"answer", sessionID:"s", type:"reasoning", text:"think"}} }
    await events.event(reasoning)
    await events.event({type:"tool.execute.before", properties:{sessionID:"s", callID:"call", tool:"bash", args:{command:"echo hi", api_key:"secret"}}})
    await events.event({type:"unknown.event", properties:{data:"x".repeat(240001)}})
    await events.flush()
    assert.deepEqual(sent.map(e => e.event.type), ["session.updated","message.updated","message.part.updated","tool.execute.before","unknown.event"])
    assert.equal(sent[2].request_id, "request")
    assert.equal(sent[2].event.properties.part.text, "think")
    assert.equal(logged[2].chat_id,"chat")
    assert.ok(!logged[3].data.includes("secret"))
    assert.equal(sent[4].event.properties.data.length,240001)
  } finally { await rm(root, {recursive:true, force:true}) }
})

test("failed delivery remains on disk and retries the same ID until ACK", async () => {
  const root = await mkdtemp(join(tmpdir(), "events-")), attempts = [], logs = []
  let available = false
  try {
    const events = await createRuntimeEvents("/alice", root, async body => {
      attempts.push(body.event_id)
      if (!available) throw new Error("offline")
    }, body => logs.push(body.event_id))
    await events.event({type:"test",properties:{}})
    await new Promise(resolve=>setTimeout(resolve,50))
    const folder=join(root,createHash("sha256").update("/alice").digest("hex"))
    assert.equal((await readdir(folder)).filter(f=>f.endsWith(".json")).length,1)
    available=true
    await events.flush()
    assert.ok(attempts.length >= 2)
    assert.equal(new Set(attempts).size,1)
    assert.equal(logs.length,1)
    assert.deepEqual(await readdir(folder),[])
  } finally { await rm(root,{recursive:true,force:true}) }
})

test("process restart drains previously persisted native events", async () => {
  const root=await mkdtemp(join(tmpdir(),"events-")), sent=[]
  try {
    const folder=join(root,createHash("sha256").update("/alice").digest("hex"))
    await mkdir(folder)
    const body={type:"opencode.event",event_id:"original",directory:"/alice",event:{type:"test",properties:{}}}
    await writeFile(join(folder,"00000000000000000001.json"),JSON.stringify(body))
    const events=await createRuntimeEvents("/alice",root,async body=>sent.push(body),()=>{})
    await events.flush()
    assert.deepEqual(sent,[body])
  } finally {await rm(root,{recursive:true,force:true})}
})

test("tool hook preserves original event arguments and uses runtime authorization", async () => {
  const root=await mkdtemp(join(tmpdir(),"plugin-")), savedFetch=globalThis.fetch, savedEnv={...process.env}, calls=[]
  Object.assign(process.env,{BUCKET_RPC_URL:"http://127.0.0.1:4097",BUCKET_RPC_TOKEN:"token",
    BUCKET_EXEC_PYTHON:"/python",BUCKET_EXEC_CLIENT:"/client.py",BUCKET_EVENT_ROOT:root,
    BUCKET_SESSION_LOGGING:JSON.stringify({...settings,enabled:false})})
  globalThis.fetch=async (url, options)=>{
    const body=JSON.parse(options.body);calls.push({url,body})
    return new Response(JSON.stringify(url.endsWith("/authorize") ? {ticket:"authorized",host_workdir:"/alice"}:{ok:true}),{status:200})
  }
  try {
    const plugin=await BucketRuntimePlugin({directory:"/alice"})
    const output={args:{command:"echo original",timeout:1000}}
    await plugin["tool.execute.before"]({sessionID:"s",callID:"c",tool:"bash"},output)
    await plugin.dispose()
    assert.ok(output.args.command.includes("authorized"))
    assert.equal(output.args.workdir,"/alice")
    assert.equal(calls.find(c=>c.url.endsWith("/plugin/event")).body.event.properties.args.command,"echo original")
    assert.equal(calls.find(c=>c.url.endsWith("/plugin/ready")).body.version,"4")
    assert.ok(!("caption" in plugin.tool.publish_attachment.args))
  } finally {globalThis.fetch=savedFetch;process.env=savedEnv;await rm(root,{recursive:true,force:true})}
})


test("unfinished spool writes cannot block the next event after restart", async () => {
  const root=await mkdtemp(join(tmpdir(),"events-")), sent=[]
  try {
    const folder=join(root,createHash("sha256").update("/alice").digest("hex"))
    await mkdir(folder)
    await writeFile(join(folder,"00000000000000000001.json.tmp"),"incomplete")
    const events=await createRuntimeEvents("/alice",root,async body=>sent.push(body),()=>{})
    await events.event({type:"next",properties:{}})
    await events.flush()
    assert.equal(sent.length,1)
    assert.equal(sent[0].event.type,"next")
    assert.deepEqual(await readdir(folder),[])
  } finally {await rm(root,{recursive:true,force:true})}
})

test("session errors follow the active user input and idle clears that association", async () => {
  const root=await mkdtemp(join(tmpdir(),"events-")), sent=[]
  try {
    const events=await createRuntimeEvents("/alice",root,async body=>sent.push(body),()=>{})
    await events.event(session)
    await events.event({type:"message.updated",properties:{info:{id:"input",role:"user",sessionID:"s"}}})
    await events.event({type:"session.error",properties:{sessionID:"s",error:{name:"ProviderError"}}})
    await events.event({type:"session.idle",properties:{sessionID:"s"}})
    await events.event({type:"session.error",properties:{sessionID:"s",error:{name:"UnrelatedError"}}})
    await events.flush()
    assert.equal(sent[2].request_id,"request")
    assert.equal(sent[2].input_message_id,"input")
    assert.equal(sent[4].request_id,"")
  } finally {await rm(root,{recursive:true,force:true})}
})
