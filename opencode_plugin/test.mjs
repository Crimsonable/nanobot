import assert from "node:assert/strict"
import { test } from "node:test"
import { spawnSync } from "node:child_process"
import * as fs from "node:fs"
import * as os from "node:os"
import * as path from "node:path"
import { applyPermissions } from "./dist/permissions.mjs"
import { BucketRuntimePlugin } from "./dist/index.mjs"

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "plugin-policy-"))
  t.after(() => fs.rmSync(root, {recursive:true, force:true}))
  const workspace = path.join(root, "alice"), publicRoot = path.join(root, "public")
  fs.mkdirSync(workspace); fs.mkdirSync(publicRoot)
  return {root, workspace, publicRoot, context: {
    schema_version:1, frontend_id:"web", user_id:"alice", chat_id:"chat",
    input_message_id:"input", requests:{input:"req"}, execution:{
      version:1, workspace, deadline_at:Date.now()/1000+60,
      policy:{session:{enabled:true, default_tools:true, tools:{}},
        filesystem:{workspace:"read_write"}, sandbox:{enabled:true, network:false}},
      public_roots:[publicRoot], private_files:[], lock_path:path.join(root,"lock"),
      sandbox:{binary:"/usr/bin/bwrap", shell:"/bin/sh", readonly_roots:["/usr","/bin","/lib","/lib64"]},
    },
  }}
}

test("hook queries BU scope; publication calls its tool endpoint without preauthorization", async t => {
  const {workspace, context} = fixture(t)
  const savedFetch=globalThis.fetch, savedEnv={...process.env}, calls=[]
  t.after(() => {globalThis.fetch=savedFetch;process.env=savedEnv})
  Object.assign(process.env,{BUCKET_RPC_URL:"http://127.0.0.1:4097",BUCKET_RPC_TOKEN:"token"})
  globalThis.fetch=async (url, options)=>{
    calls.push({url, body:JSON.parse(options.body)})
    assert.ok(!url.endsWith("/authorize"))
    return new Response(JSON.stringify(url.endsWith("/permissions") ? context.execution : {ok:true}))
  }
  const plugin=await BucketRuntimePlugin({directory:workspace})
  const command="a'b\"c; $(printf EXPANDED) `printf EXPANDED`\n> /tmp/should-not-be-created"
  const output={args:{command,timeout:1000}}
  await plugin["tool.execute.before"]({sessionID:"s",callID:"c",tool:"bash"},output)
  assert.ok(output.args.command.startsWith("exec '/usr/bin/flock'"))
  assert.equal(output.args.workdir,workspace)
  assert.equal(output.args.timeout,1000)
  assert.equal(plugin.event,undefined)
  assert.ok(calls.every(c=>!c.url.endsWith("/plugin/event")))
  assert.equal(calls.find(c=>c.url.endsWith("/plugin/ready")).body.version,"9")
  const messages={messages:[{parts:[{type:"tool",tool:"bash",state:{input:output.args}}]}]}
  const restarted=await BucketRuntimePlugin({directory:workspace})
  await restarted["experimental.chat.messages.transform"]({},messages)
  assert.equal(messages.messages[0].parts[0].state.input.command,command)
  // Validate shell quoting separately from actually executing the user command.
  if (process.platform === "linux") {
    const suffix=output.args.command.slice(output.args.command.lastIndexOf(" '-c' ")+6)
    const result=spawnSync("/bin/sh",["-c","printf '%s' "+suffix],{encoding:"utf8"})
    assert.equal(result.status,0,result.stderr);assert.equal(result.stdout,command)
  }
  context.execution.policy.filesystem.workspace="read_only"
  await assert.rejects(plugin["tool.execute.before"]({sessionID:"s",tool:"write"},{args:{filePath:"no"}}),/permission scope/)
  assert.ok(!("_runtime_ticket" in plugin.tool.publish_attachment.args))
  context.execution.policy.filesystem.workspace="read_write"
  const beforePublish=calls.length
  await plugin["tool.execute.before"]({sessionID:"s",callID:"pub",tool:"publish_attachment"},{args:{files:[{path:"report.txt"}]}})
  assert.deepEqual(calls.slice(beforePublish).map(c=>new URL(c.url).pathname),[])
  await plugin.tool.publish_attachment.execute({files:[{path:"report.txt"}]},
    {sessionID:"s",messageID:"assistant",abort:new AbortController().signal})
  assert.deepEqual(calls.find(c=>c.url.endsWith("/publish")).body,
    {session_id:"s",directory:workspace,message_id:"assistant",files:[{path:"report.txt"}]})
})

test("BU scope enforces native paths, patches, links, tool switches and publication scope",t=>{
  const {root,workspace,publicRoot,context}=fixture(t)
  assert.equal(applyPermissions(context.execution,"read",{filePath:"a"}).args.filePath,path.join(workspace,"a"))
  assert.throws(()=>applyPermissions(context.execution,"read",{filePath:path.join(root,"bob","a")}),/permission scope/)
  assert.throws(()=>applyPermissions(context.execution,"write",{filePath:path.join(publicRoot,"a")}),/permission scope/)
  assert.throws(()=>applyPermissions(context.execution,"apply_patch",{patchText:"*** Move to: ../bob/a"}),/permission scope/)
  assert.throws(()=>applyPermissions(context.execution,"glob",{pattern:"../*"}),/Glob/)
  context.execution.private_files=[path.join(publicRoot,"secret")]
  assert.throws(()=>applyPermissions(context.execution,"grep",{path:publicRoot,pattern:"x"}),/configuration/)
  context.execution.policy.session.tools.read=false
  assert.throws(()=>applyPermissions(context.execution,"read",{filePath:"a"}),/disabled/)
  context.execution.policy.session.tools.read=true
  if (process.platform === "linux") {
    fs.symlinkSync(path.join(root,"missing"),path.join(workspace,"link"))
    assert.throws(()=>applyPermissions(context.execution,"read",{filePath:"link/new"}),/Symbolic/)
    fs.writeFileSync(path.join(root,"original"),"private")
    fs.linkSync(path.join(root,"original"),path.join(workspace,"hard"))
    assert.throws(()=>applyPermissions(context.execution,"read",{filePath:"hard"}),/Hard-linked/)
  }
})

test("shell scope obeys read-only, no-access, network and deadlines",t=>{
  const {workspace,context}=fixture(t)
  for (const timeout of [0,-1,NaN,Infinity,true,"100"]) assert.throws(()=>applyPermissions(context.execution,"bash",{command:"pwd",timeout}),/timeout/)
  context.execution.policy.filesystem.workspace="read_only"
  let result=applyPermissions(context.execution,"bash",{command:"pwd",timeout:999999})
  assert.ok(!result.argv.includes("--bind"));assert.ok(!result.argv.includes("--share-net"))
  assert.ok(result.timeout_ms<=60000)
  context.execution.policy.filesystem.workspace="none"
  result=applyPermissions(context.execution,"bash",{command:"pwd"})
  assert.ok(!result.argv.includes(workspace))
  assert.equal(result.argv[result.argv.indexOf("--chdir")+1],"/")
  assert.throws(()=>applyPermissions(context.execution,"bash",{command:"pwd",workdir:workspace}),/permission scope/)
  context.execution.deadline_at=1
  assert.throws(()=>applyPermissions(context.execution,"bash",{command:"pwd"}),/deadline/)
})
