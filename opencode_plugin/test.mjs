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
import { createHash } from "node:crypto"

test("deployed model configuration uses only native V2 fields",()=>{
  const config=JSON.parse(fs.readFileSync(new URL("../config/opencode/web-test/config/opencode-server.json",import.meta.url),"utf8"))
  Schema.decodeUnknownSync(Config.Info)(config,{onExcessProperty:"error"})
})

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "plugin-policy-"))
  t.after(() => fs.rmSync(root, {recursive:true, force:true}))
  const workspace = path.join(root, "users", "web", "alice"), publicRoot = path.join(root, "common", "web", "app")
  fs.mkdirSync(workspace,{recursive:true}); fs.mkdirSync(publicRoot,{recursive:true})
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

test("plugin reads native input identity and mounted policy without a service RPC", async t => {
  const {root,workspace,publicRoot,context} = fixture(t)
  const savedFetch=globalThis.fetch, savedEnv={...process.env}, calls=[]
  t.after(() => {globalThis.fetch=savedFetch;process.env=savedEnv})
  const permissions=path.join(root,"permissions.yaml")
  fs.writeFileSync(permissions,JSON.stringify({defaults:context.execution.policy}))
  process.env.MANAGED_BUCKET=JSON.stringify({behavior:"pool",frontend_id:"web",bucket_id:"b",permissions_file:permissions,
    common_root:path.join(root,"common"),workspace_root:path.join(root,"users"),state_root:path.join(root,"state"),
    opencode:{port:4096,config_file:path.join(root,"model.json"),plugin_dir:path.join(root,"plugin"),context_root:path.join(root,"context")},
    sandbox:context.execution.sandbox,limits:{file_bytes:1000,spool_bytes:10000}})
  globalThis.fetch=async url=>{
    calls.push(String(url))
    return new Response(JSON.stringify({data:[
      {id:"assistant",type:"assistant",time:{created:2}},
      {id:"input",type:"user",time:{created:1},metadata:{managed:{request_id:"req",deadline_at:Date.now()/1000+60}}}],cursor:{}}))
  }
  const hooks={},tools={},removed=[]
  const ctx={location:{directory:workspace},
    session:{get:async()=>({id:"s",location:{directory:workspace},metadata:{managed:{frontend_id:"web",user_id:"alice",identity_id:"identity"}}}),
      hook:async(name,fn)=>{const previous=hooks[name]; hooks[name]=async event=>{await previous?.(event); await fn(event)}}},
    permission:{hook:async(name,fn)=>{hooks["permission."+name]=fn}},
    skill:{transform:async(fn)=>fn({list:()=>[],remove:()=>{}})},
    tool:{hook:async(name,fn)=>{hooks[name]=fn},transform:async(fn)=>fn({
      list:()=>[{id:"opencode_session_move",options:{namespace:"opencode"}}, {id:"read"}],
      remove:name=>removed.push(name),add:tool=>{tools[tool.name]=tool}})}}
  await plugin.setup(ctx)
  assert.deepEqual(removed,["opencode_session_move"])
  assert.equal(tools.publish_attachment.options.codemode,false)
  const command="printf 'literal $HOME `whoami`'"
  const event={sessionID:"s",messageID:"assistant",tool:"shell",input:{command,timeout:1000,background:true}}
  await hooks["execute.before"](event)
  assert.ok(event.input.command.startsWith("exec '/usr/bin/timeout' '--kill-after=1s' '1.000s'"))
  assert.equal(event.input.workdir,workspace)
  assert.equal(event.input.background,false)
  const messages={sessionID:"s",tools:{},system:[],messages:[{role:"assistant",content:[{type:"tool-call",name:"shell",id:"c",input:event.input}]}]}
  await hooks.context(messages)
  assert.equal(messages.messages[0].content[0].input.command,command)
  fs.writeFileSync(path.join(workspace,"AGENTS.md"),"updated rules")
  const fresh={sessionID:"s",tools:{},system:[],messages:[]}
  await hooks.context(fresh)
  assert.ok(fresh.system[0].text.endsWith("updated rules"))
  await assert.rejects(hooks["execute.before"]({sessionID:"s",tool:"opencode_session_move",input:{directory:"/"}}),/Server control/)
  fs.writeFileSync(permissions,"defaults:\n  filesystem:\n    workspace: read_only\n")
  await assert.rejects(hooks["execute.before"]({sessionID:"s",tool:"write",input:{path:"no"}}),/permission scope/)
  assert.ok(calls.every(url=>url.startsWith("http://127.0.0.1:4096/api/session/s/message")))
})

test("native skill refresh excludes another frontend and service files",t=>{
  const {workspace,publicRoot,root,context}=fixture(t)
  assert.equal(visibleSkill(path.join(publicRoot,"skills/SKILL.md"),workspace,publicRoot),true)
  assert.equal(visibleSkill(path.join(workspace,".agents/skills/SKILL.md"),workspace,publicRoot),true)
  assert.equal(visibleSkill(path.join(root,"other/SKILL.md"),workspace,publicRoot),false)
  assert.equal(visibleSkill("skill://builtin/SKILL.md",workspace,publicRoot),true)
  assert.equal(applyPermissions(context.execution,"read",{path:"skill://builtin/SKILL.md"}).args.path,"skill://builtin/SKILL.md")
})

test("Plugin scope enforces native paths, patches, links, tool switches and publication scope",t=>{
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

test("user policy merges defaults, isolates frontends, and reloads edits",async t=>{
  const {policyFor}=await import("./dist/policy.mjs")
  const {root}=fixture(t), file=path.join(root,"policy.yaml")
  fs.writeFileSync(file,JSON.stringify({defaults:{session:{tools:{shell:false,read:true}}},users:[
    {frontend_id:"web",user_id:"alice",session:{tools:{shell:true}}},
    {frontend_id:"other",user_id:"alice",filesystem:{workspace:"none"}}]}))
  const policy=policyFor(file,"web","alice")
  assert.equal(policy.session.tools.shell,true)
  assert.equal(policy.session.tools.read,true)
  assert.equal(policy.filesystem.workspace,"read_write")
  fs.writeFileSync(file,'defaults: {sandbox: {enabled: false}}')
  assert.throws(()=>policyFor(file,"web","alice"),/isolation/)
})

test("individual uses native shell input unchanged and allows mounted config paths",async t=>{
  const {root,workspace}=fixture(t), hooks={}, tools={}
  const savedEnv={...process.env}, savedFetch=globalThis.fetch
  t.after(()=>{process.env=savedEnv;globalThis.fetch=savedFetch})
  const owner={frontend_id:"web",user_id:"alice"}
  const identity_id=createHash("sha256").update(JSON.stringify(owner)).digest("hex")
  const configFile=path.join(root,"common/web/config/model.json")
  fs.mkdirSync(path.dirname(configFile),{recursive:true})
  fs.writeFileSync(configFile,'{"model":"test"}')
  const permissions=path.join(path.dirname(configFile),"user-permissions.json")
  fs.writeFileSync(permissions,JSON.stringify({defaults:{session:{tools:{webfetch:false}},filesystem:{workspace:"read_only"}}}))
  const config={behavior:"individual",frontend_id:"web",bucket_id:"opencode-user-"+identity_id.slice(0,32),owner,
    user_workspace:workspace,common_root:path.join(root,"common"),workspace_root:path.join(root,"users"),state_root:path.join(root,"state"),
    permissions_file:permissions,opencode:{port:4096,config_file:configFile},limits:{file_bytes:1000,spool_bytes:10000}}
  process.env.MANAGED_BUCKET=JSON.stringify(config)
  let sessionOwner={...owner,identity_id}, deadline=Date.now()/1000+60
  globalThis.fetch=async()=>new Response(JSON.stringify({data:[{id:"input",type:"user",
    metadata:{managed:{request_id:"req",deadline_at:deadline}}}],cursor:{}}))
  const ctx={location:{directory:workspace},
    session:{get:async()=>({id:"s",location:{directory:workspace},metadata:{managed:sessionOwner}}),
      hook:async(name,fn)=>{const previous=hooks[name];hooks[name]=async event=>{await previous?.(event);await fn(event)}}},
    permission:{hook:async(name,fn)=>{hooks["permission."+name]=fn}},
    skill:{transform:async()=>{throw new Error("individual must not filter paths with pooled policy")}},
    tool:{hook:async(name,fn)=>{hooks[name]=fn},transform:async fn=>fn({list:()=>[],remove:()=>{},add:tool=>{tools[tool.name]=tool}})}}
  await plugin.setup(ctx)
  for (const input of [{command:"printf '%s' '$HOME `whoami`'",workdir:workspace,timeout:7000,background:true},
                       {command:"cat "+configFile}]) {
    const event={sessionID:"s",tool:"shell",input}, before=structuredClone(event)
    await hooks["execute.before"](event)
    assert.deepEqual(event,before)
    assert.equal(event.input,input)
  }
  for (const tool of ["read","write","edit"]) {
    const event={sessionID:"s",tool,input:{path:configFile}}, before=structuredClone(event)
    await hooks["execute.before"](event)
    assert.deepEqual(event,before) // the mount, rather than a plugin path check, rejects writes
  }
  const permission={sessionID:"s",action:"external_directory",effect:"ask"}
  await hooks["permission.evaluate"](permission)
  assert.equal(permission.effect,"allow")
  assert.ok(tools.publish_attachment)
  await assert.rejects(hooks["execute.before"]({sessionID:"s",tool:"webfetch",input:{}}),/disabled/)
  fs.writeFileSync(permissions,JSON.stringify({defaults:{session:{tools:{shell:false,webfetch:true}}}}))
  await hooks["execute.before"]({sessionID:"s",tool:"webfetch",input:{}})
  await assert.rejects(hooks["execute.before"]({sessionID:"s",tool:"shell",input:{command:"pwd"}}),/disabled/)
  fs.writeFileSync(permissions,"{}")
  sessionOwner={...sessionOwner,user_id:"bob"}
  await assert.rejects(hooks["execute.before"]({sessionID:"s",tool:"shell",input:{command:"pwd"}}),/owner mismatch/)
  sessionOwner={...owner,identity_id}
  deadline=1
  await assert.rejects(hooks["execute.before"]({sessionID:"s",tool:"shell",input:{command:"pwd"}}),/deadline/)
})

test("individual policy supports optional files, owner overrides and live JSON grants",async t=>{
  const {policyFor}=await import("./dist/policy.mjs")
  const {root}=fixture(t), file=path.join(root,"permissions.json"), mounts=[{name:"docs"},{name:"media"}]
  assert.equal(policyFor(null,"web","alice","individual").filesystem.workspace,"read_write")
  fs.writeFileSync(file,JSON.stringify({defaults:{filesystem:{mounts:{docs:"read_only",media:"read_only"}},session:{tools:{shell:false}}},users:[
    {user_id:"alice",filesystem:{mounts:{docs:"read_write"}},session:{tools:{shell:true}}},
    {frontend_id:"other",user_id:"alice",filesystem:{workspace:"none"}}]}))
  const policy=policyFor(file,"web","alice","individual",mounts)
  assert.equal(policy.session.tools.shell,true)
  assert.deepEqual(policy.filesystem.mounts,{docs:"read_write",media:"read_only"})
  assert.equal(policy.filesystem.workspace,"read_write")
  assert.equal(policy.sandbox.enabled,false)
  fs.writeFileSync(file,'{"defaults":{"sandbox":{"network":false}}}')
  assert.throws(()=>policyFor(file,"web","alice","individual",mounts),/network=false/)
})
