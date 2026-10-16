import assert from "node:assert/strict"
import { test } from "node:test"
import { spawnSync } from "node:child_process"
import { createRuntimeEvents } from "./dist/runtime-events.mjs"
import { BucketRuntimePlugin } from "./dist/index.mjs"

test("native events are forwarded unchanged with only transport metadata", async () => {
  const sent = []
  const events = createRuntimeEvents("/alice", async body => sent.push(body))
  const native = {type:"message.part.updated",properties:{part:{id:"p",sessionID:"s",messageID:"m",type:"reasoning",text:"思考"}}}
  await events.event(native)
  await events.event({type:"future.event",properties:{text:"x".repeat(240001)}})
  assert.deepEqual(sent[0].event,native)
  assert.deepEqual(Object.keys(sent[0]).sort(),["directory","event","event_id","type"])
  assert.equal(sent[1].event.properties.text.length,240001)
})

test("wrapper passes shell metacharacters as one literal argument and fails closed", async () => {
  const savedFetch = globalThis.fetch, savedEnv = {...process.env}
  Object.assign(process.env, {BUCKET_RPC_URL:"http://127.0.0.1:4097", BUCKET_RPC_TOKEN:"token"})
  let authorization = {argv:["/usr/bin/printf", "%s"], host_workdir:"/alice", timeout_ms:1000}
  globalThis.fetch = async url => new Response(JSON.stringify(url.endsWith("/authorize") ? authorization : {ok:true}))
  try {
    const plugin = await BucketRuntimePlugin({directory:"/alice"})
    const command = "a'b\"c; $(printf EXPANDED) `printf EXPANDED`\n> /tmp/should-not-be-created"
    const output = {args:{command}}
    await plugin["tool.execute.before"]({sessionID:"s",callID:"c",tool:"bash"}, output)
    const result = process.platform === "win32"
      ? spawnSync("wsl.exe", ["--exec", "/bin/sh", "-c", output.args.command], {encoding:"utf8"})
      : spawnSync("/bin/sh", ["-c", output.args.command], {encoding:"utf8"})
    assert.equal(result.status, 0, result.stderr)
    assert.equal(result.stdout, command)
    authorization = {argv:["/usr/bin/flock", "--exclusive", "--close", "/private/lock",
      "/usr/bin/bwrap", "--unshare-all", "--clearenv", "--chdir", "/alice/a'b", "--", "/bin/sh", "-c"],
      host_workdir:"/alice", timeout_ms:1000}
    const wrapped = {args:{command}}
    await plugin["tool.execute.before"]({sessionID:"s",callID:"next",tool:"bash"}, wrapped)
    const context = {messages:[{parts:[{type:"tool", tool:"bash", state:{input:wrapped.args}}]}]}
    // A fresh plugin has no in-memory call cache, just like a restarted server.
    const restarted = await BucketRuntimePlugin({directory:"/alice"})
    await restarted["experimental.chat.messages.transform"]({}, context)
    assert.equal(context.messages[0].parts[0].state.input.command, command)
    assert.equal(context.messages[0].parts[0].state.input.workdir, "/alice/a'b")
    authorization = {ticket:"obsolete"}
    const untouched = {args:{command:"echo original"}}
    await assert.rejects(plugin["tool.execute.before"]({sessionID:"s",callID:"bad",tool:"bash"}, untouched), /Invalid sandbox authorization/)
    assert.equal(untouched.args.command, "echo original")
  } finally { globalThis.fetch = savedFetch; process.env = savedEnv }
})

test("failed delivery rejects immediately without retrying or blocking the next event", async () => {
  const sent=[]
  const events=createRuntimeEvents("/alice",async body=>{
    sent.push(body)
    if(body.event.type==="first") throw new Error("offline")
  })
  await assert.rejects(events.event({type:"first"}),/offline/)
  await events.event({type:"second"})
  assert.deepEqual(sent.map(body=>body.event.type),["first","second"])
})

test("tool hook preserves original event arguments and uses runtime authorization", async () => {
  const savedFetch=globalThis.fetch, savedEnv={...process.env}, calls=[]
  Object.assign(process.env,{BUCKET_RPC_URL:"http://127.0.0.1:4097",BUCKET_RPC_TOKEN:"token"})
  globalThis.fetch=async (url, options)=>{
    const body=JSON.parse(options.body);calls.push({url,body})
    return new Response(JSON.stringify(url.endsWith("/authorize") ? {argv:["/usr/bin/bwrap","--","/bin/sh","-c"],host_workdir:"/alice",timeout_ms:1000}:{ok:true}),{status:200})
  }
  try {
    const plugin=await BucketRuntimePlugin({directory:"/alice"})
    const output={args:{command:"echo original",timeout:1000}}
    await plugin["tool.execute.before"]({sessionID:"s",callID:"c",tool:"bash"},output)
    assert.equal(output.args.command, "exec '/usr/bin/bwrap' '--' '/bin/sh' '-c' 'echo original'")
    assert.equal(output.args.timeout,1000)
    assert.ok(!calls.some(c=>c.url.endsWith("/execute")))
    assert.equal(output.args.workdir,"/alice")
    assert.equal(calls.find(c=>c.url.endsWith("/plugin/event")).body.event.properties.args.command,"echo original")
    assert.equal(calls.find(c=>c.url.endsWith("/plugin/ready")).body.version,"6")
    assert.ok(!("caption" in plugin.tool.publish_attachment.args))
  } finally {globalThis.fetch=savedFetch;process.env=savedEnv}
})
