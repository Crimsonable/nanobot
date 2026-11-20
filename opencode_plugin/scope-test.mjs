// Cross-language tests exercise the actual plugin permission implementation.
import { applyPermissions } from "./dist/permissions.mjs"
let text = ""
for await (const chunk of process.stdin) text += chunk
const {scope, tool, args, hook = false} = JSON.parse(text)
try {
  if (!hook) process.stdout.write(JSON.stringify(applyPermissions(scope, tool, args)))
  else {
    // Exercise the production hook, including its timeout/quoting wrapper, while
    // replacing only the private RPC transport with the supplied test scope.
    const {default: plugin} = await import("./dist/index.mjs")
    process.env.BUCKET_RPC_URL = "http://127.0.0.1:1"
    process.env.BUCKET_RPC_TOKEN = "test-only"
    process.env.BUCKET_PUBLIC_ROOT = scope.public_roots[0]
    globalThis.fetch = async url => new Response(JSON.stringify(url.endsWith("/permissions") ? scope : {ok:true}))
    const hooks = {}
    await plugin.setup({location:{directory:scope.workspace},
      session:{hook:async()=>{}}, skill:{transform:async()=>{}},
      tool:{hook:async(name,fn)=>{hooks[name]=fn}, transform:async()=>{}}})
    const event = {sessionID:"test",tool,input:args}
    await hooks["execute.before"](event)
    process.stdout.write(JSON.stringify(event.input))
  }
}
catch (error) { process.stderr.write(error.message); process.exitCode = 1 }
