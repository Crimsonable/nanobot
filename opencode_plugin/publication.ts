import * as fs from "node:fs"
import * as path from "node:path"
import { fileURLToPath } from "node:url"
import { spawn } from "node:child_process"
import { createHash } from "node:crypto"

const pending = new Map<string, Promise<any>>()

export async function publication(config: any, scope: any, files: any[], context: any) {
  const key = JSON.stringify([context.sessionID,context.messageID,files])
  if (pending.has(key)) return pending.get(key)
  const task = publish(config,scope,files,context)
  pending.set(key,task)
  try {return await task} finally {pending.delete(key)}
}

async function publish(config: any, scope: any, files: any[], context: any) {
  const gateway = config.gateway_url, bucket = config.bucket_id
  const root = path.join(config.state_root, "publications"), python = process.env.PYTHON_BINARY ?? "/usr/local/bin/python"
  const owner = scope.owner, rootSession = scope.root_session, requestID = scope.request_id, policy = scope.policy
  if ((policy.session.tools.publish_attachment ?? policy.session.default_tools) !== true ||
      policy.filesystem.workspace === "none") throw new Error("Attachment publication is not permitted")
  if (!Array.isArray(files) || files.length < 1 || files.length > 32 || !context.messageID)
    throw new Error("Invalid publication")
  const remaining = scope.deadline_at * 1000 - Date.now()
  if (!Number.isFinite(remaining) || remaining <= 0) throw new Error("Request deadline expired")
  const signal = AbortSignal.any([context.signal, AbortSignal.timeout(Math.ceil(remaining))])
  signal.throwIfAborted()
  const key = createHash("sha256").update(JSON.stringify([requestID,context.messageID,files])).digest("hex")
  const destination = path.join(root,key), resultPath = path.join(destination,"result.json")
  if (fs.existsSync(resultPath)) return JSON.parse(fs.readFileSync(resultPath,"utf8"))
  const attachments: any[] = await new Promise((resolve,reject) => {
    const child = spawn(python, [fileURLToPath(new URL("../snapshot.py",import.meta.url))],
      {stdio:["pipe","pipe","pipe"], detached:process.platform !== "win32"})
    let output = "", error = ""
    const cancel = () => {try {process.kill(process.platform === "win32" ? child.pid! : -child.pid!,"SIGKILL")} catch {}}
    signal.addEventListener("abort",cancel,{once:true})
    child.stdout.setEncoding("utf8").on("data",data=>{output+=data})
    child.stderr.setEncoding("utf8").on("data",data=>{error+=data})
    child.on("error",reject)
    child.stdin.on("error",reject)
    child.on("close",code=>{
      signal.removeEventListener("abort",cancel)
      if (signal.aborted) return reject(signal.reason)
      if (code !== 0) return reject(new Error(error || "Publication snapshot failed"))
      try {resolve(JSON.parse(output))} catch (error) {reject(error)}
    })
    child.stdin.end(JSON.stringify({scope,root,key,files}))
  })
  const published = []
  for (const attachment of attachments) {
    const url = new URL(`/internal/attachments/${[owner.frontend_id,requestID,attachment.attachment_id].map(encodeURIComponent).join("/")}`,gateway)
    for (const name of ["filename","content_type","sha256"]) url.searchParams.set(name,attachment[name])
    const input = fs.createReadStream(attachment.path)
    try {
      const response = await fetch(url,{method:"POST", headers:{"x-bucket-id":bucket,
        "x-root-session-id":rootSession!,"content-type":"application/octet-stream"},
        body:input as any, duplex:"half", signal} as any)
      if (!response.ok) throw new Error(`Gateway upload rejected: ${response.status}`)
      const result = await response.json()
      for (const name of ["attachment_id","filename","size","sha256"])
        if (result[name] !== attachment[name]) throw new Error("Gateway returned a different attachment")
      published.push(result)
    } finally {input.destroy()}
  }
  const result = {status:"queued_for_reply",attachments:published}
  fs.writeFileSync(resultPath+".tmp",JSON.stringify(result))
  fs.renameSync(resultPath+".tmp",resultPath)
  for (const item of attachments) fs.rmSync(item.path,{force:true})
  return result
}
