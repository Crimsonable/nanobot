import assert from "node:assert/strict"
import { test } from "node:test"
import { BucketRuntimePlugin } from "./dist/index.mjs"

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
    const hooks = await BucketRuntimePlugin({})
    const output = { args: { command: "echo $(cat /secret)", workdir: "/other-user" } }
    await hooks["tool.execute.before"]({ tool: "bash", sessionID: "session-a", callID: "call-a" }, output)
    assert.equal(output.args.command, "'/usr/bin/python3' '/opt/app/exec_client.py' 'trusted-ticket'")
    assert.equal(output.args.workdir, "/opt/context")
    assert.equal(calls[1].body.session_id, "session-a")
    assert.equal(calls[1].body.args.command, "echo $(cat /secret)")
    const publish = { args: { files: [{ path: "output/report.pdf" }], _runtime_ticket: "forged" } }
    await hooks["tool.execute.before"]({ tool: "publish_attachment", sessionID: "session-a", callID: "call-b" }, publish)
    assert.equal(publish.args._runtime_ticket, "trusted-ticket")
    assert.equal(calls[2].body.args._runtime_ticket, undefined)
    const permission = { status: "allow" }
    await hooks["permission.ask"]({}, permission)
    assert.equal(permission.status, "deny")
    await assert.rejects(() => hooks["command.execute.before"](), /disabled/)
  } finally {
    globalThis.fetch = savedFetch
    for (const key of Object.keys(process.env)) if (!(key in savedEnv)) delete process.env[key]
    Object.assign(process.env, savedEnv)
  }
})
