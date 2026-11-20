import assert from "node:assert/strict"
import { test } from "node:test"
import { spawnSync } from "node:child_process"
import * as fs from "node:fs"
import * as os from "node:os"
import * as path from "node:path"
import { applyPermissions, visibleSkill, workspaceInstructions } from "./dist/permissions.mjs"
import plugin from "./dist/index.mjs"
import { Schema } from "effect"
import { Config } from "@opencode/schema/config"

test("deployed model configuration uses only native V2 fields",()=>{
  const config=JSON.parse(fs.readFileSync(new URL("../config/opencode/web-test/config/opencode-server.json",import.meta.url),"utf8"))
  Schema.decodeUnknownSync(Config.Info)(config,{onExcessProperty:"error"})
})

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

test("V2 registration, shell deadlines, context normalization and publication", async t => {
  const {workspace,publicRoot,context} = fixture(t)
  const savedFetch=globalThis.fetch, savedEnv={...process.env}, calls=[]
  t.after(() => {globalThis.fetch=savedFetch;process.env=savedEnv})
  Object.assign(process.env,{BUCKET_RPC_URL:"http://127.0.0.1:4097",BUCKET_RPC_TOKEN:"token",BUCKET_PUBLIC_ROOT:publicRoot})
  globalThis.fetch=async (url, options)=>{
    calls.push({url, body:JSON.parse(options.body)})
    return new Response(JSON.stringify(url.endsWith("/permissions") ? context.execution : {ok:true}))
  }
  const hooks={},tools={},removed=[]
  const ctx={location:{directory:workspace},
    session:{hook:async(name,fn)=>{const previous=hooks[name]; hooks[name]=async event=>{await previous?.(event); await fn(event)}}},
    skill:{transform:async(fn)=>fn({list:()=>[],remove:()=>{}})},
    tool:{hook:async(name,fn)=>{hooks[name]=fn},transform:async(fn)=>fn({
      list:()=>[{id:"opencode_session_move",options:{namespace:"opencode"}},
        {id:"opencode_session_rename",options:{namespace:"opencode"}}, {id:"read"}],
      remove:name=>removed.push(name),add:tool=>{tools[tool.name]=tool}})}}
  await plugin.setup(ctx)
  assert.deepEqual(removed,["opencode_session_move","opencode_session_rename"])
  assert.equal(tools.publish_attachment.options.codemode,false)
  await hooks["experimental.idle"]({sessionID:"s",background:[{id:"child"}]})
  assert.deepEqual(calls.at(-1).body,{session_id:"s",directory:workspace,background:true})
  await hooks["experimental.idle"]({sessionID:"s",background:[]})
  assert.equal(calls.at(-1).body.background,false)
  const command="printf 'literal $HOME `whoami`'"
  const event={sessionID:"s",id:"c",tool:"shell",input:{command,timeout:1000,background:true}}
  await hooks["execute.before"](event)
  assert.ok(event.input.command.startsWith("exec '/usr/bin/timeout' '--kill-after=1s' '1.000s'"))
  assert.equal(event.input.workdir,workspace)
  assert.equal(event.input.timeout,3000)
  assert.equal(event.input.background,false)
  assert.equal(calls.find(c=>c.url.endsWith("/plugin/ready")).body.version,"10")
  const messages={system:[],messages:[{role:"assistant",content:[{type:"tool-call",name:"shell",id:"c",input:event.input}]}]}
  await hooks.context(messages)
  assert.equal(messages.messages[0].content[0].input.command,command)
  assert.deepEqual(messages.system,[])
  for (const content of ["first rules", "updated rules"]) {
    fs.writeFileSync(path.join(workspace,"AGENTS.md"),content)
    const fresh={system:[],messages:[]}
    await hooks.context(fresh)
    assert.ok(fresh.system[0].text.endsWith(content))
  }
  fs.unlinkSync(path.join(workspace,"AGENTS.md"))
  assert.equal(workspaceInstructions(workspace),undefined)
  if (process.platform !== "win32") {
    const outside=path.join(publicRoot,"rules")
    fs.writeFileSync(outside,"other rules")
    fs.symlinkSync(outside,path.join(workspace,"AGENTS.md"))
    assert.throws(()=>workspaceInstructions(workspace),/Symbolic/)
    fs.unlinkSync(path.join(workspace,"AGENTS.md"))
    fs.linkSync(outside,path.join(workspace,"AGENTS.md"))
    assert.throws(()=>workspaceInstructions(workspace),/Hard-linked/)
  }
  await assert.rejects(hooks["execute.before"]({sessionID:"s",tool:"opencode_session_move",input:{directory:"/"}}),/Server control/)
  assert.ok(calls.every(c=>!c.url.endsWith("/plugin/event")))
  context.execution.policy.filesystem.workspace="read_only"
  await assert.rejects(hooks["execute.before"]({sessionID:"s",tool:"write",input:{path:"no"}}),/permission scope/)
  const before=calls.length
  await hooks["execute.before"]({sessionID:"s",tool:"publish_attachment",input:{files:[]}})
  assert.equal(calls.length,before)
  await tools.publish_attachment.execute({files:[{path:"report.txt"}]},
    {sessionID:"s",messageID:"assistant",signal:new AbortController().signal})
  assert.deepEqual(calls.at(-1).body,{session_id:"s",directory:workspace,message_id:"assistant",files:[{path:"report.txt"}]})
})

test("native skill refresh excludes another frontend and service files",t=>{
  const {workspace,publicRoot,root,context}=fixture(t)
  assert.equal(visibleSkill(path.join(publicRoot,"skills/SKILL.md"),workspace,publicRoot),true)
  assert.equal(visibleSkill(path.join(workspace,".agents/skills/SKILL.md"),workspace,publicRoot),true)
  assert.equal(visibleSkill(path.join(root,"other/SKILL.md"),workspace,publicRoot),false)
  assert.equal(visibleSkill("skill://builtin/SKILL.md",workspace,publicRoot),true)
  assert.equal(applyPermissions(context.execution,"read",{path:"skill://builtin/SKILL.md"}).args.path,"skill://builtin/SKILL.md")
})

test("BU scope enforces native paths, patches, links, tool switches and publication scope",t=>{
  const {root,workspace,publicRoot,context}=fixture(t)
  assert.equal(applyPermissions(context.execution,"read",{path:"a"}).args.path,path.join(workspace,"a"))
  assert.throws(()=>applyPermissions(context.execution,"read",{path:path.join(root,"bob","a")}),/permission scope/)
  assert.throws(()=>applyPermissions(context.execution,"write",{path:path.join(publicRoot,"a")}),/permission scope/)
  assert.throws(()=>applyPermissions(context.execution,"patch",{patchText:"*** Move to: ../bob/a"}),/permission scope/)
  assert.throws(()=>applyPermissions(context.execution,"glob",{pattern:"../*"}),/Glob/)
  context.execution.private_files=[path.join(publicRoot,"secret")]
  assert.throws(()=>applyPermissions(context.execution,"grep",{path:publicRoot,pattern:"x"}),/configuration/)
  context.execution.policy.session.tools.read=false
  assert.throws(()=>applyPermissions(context.execution,"read",{path:"a"}),/disabled/)
  context.execution.policy.session.tools.read=true
  if (process.platform === "linux") {
    fs.symlinkSync(path.join(root,"missing"),path.join(workspace,"link"))
    assert.throws(()=>applyPermissions(context.execution,"read",{path:"link/new"}),/Symbolic/)
    fs.writeFileSync(path.join(root,"original"),"private")
    fs.linkSync(path.join(root,"original"),path.join(workspace,"hard"))
    assert.throws(()=>applyPermissions(context.execution,"read",{path:"hard"}),/Hard-linked/)
  }
})

test("shell scope obeys read-only, no-access, network and deadlines",t=>{
  const {workspace,context}=fixture(t)
  for (const timeout of [0,-1,NaN,Infinity,true,"100"]) assert.throws(()=>applyPermissions(context.execution,"shell",{command:"pwd",timeout}),/timeout/)
  context.execution.policy.filesystem.workspace="read_only"
  let result=applyPermissions(context.execution,"shell",{command:"pwd",timeout:999999})
  assert.ok(!result.argv.includes("--bind"));assert.ok(!result.argv.includes("--share-net"))
  assert.ok(result.timeout_ms<=60000)
  context.execution.policy.filesystem.workspace="none"
  result=applyPermissions(context.execution,"shell",{command:"pwd"})
  assert.ok(!result.argv.includes(workspace))
  assert.equal(result.argv[result.argv.indexOf("--chdir")+1],"/")
  assert.throws(()=>applyPermissions(context.execution,"shell",{command:"pwd",workdir:workspace}),/permission scope/)
  context.execution.deadline_at=1
  assert.throws(()=>applyPermissions(context.execution,"shell",{command:"pwd"}),/deadline/)
})
