import { build } from "esbuild"
await build({entryPoints:["index.ts","permissions.ts","policy.ts"],bundle:true,platform:"node",format:"esm",
  outdir:"dist",outExtension:{".js":".mjs"},
  banner:{js:'import { createRequire as __createRequire } from "node:module"; const require = __createRequire(import.meta.url);'}})
