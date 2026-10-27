// Cross-language tests exercise the actual plugin permission implementation.
import { applyPermissions } from "./dist/permissions.mjs"
let text = ""
for await (const chunk of process.stdin) text += chunk
const {scope, tool, args} = JSON.parse(text)
try { process.stdout.write(JSON.stringify(applyPermissions(scope, tool, args))) }
catch (error) { process.stderr.write(error.message); process.exitCode = 1 }
