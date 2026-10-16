# 隐患识别与分析 MCP 使用说明

本文档面向调用方（nanobot、业务后端、测试/运维脚本），说明当前已部署实例的接入方式、两个工具的入参出参、调用流程与常见错误。

## 1. 服务地址

| 用途 | 地址 |
| --- | --- |
| MCP 端点 | `http://192.168.48.104:48082/mcp` |
| 健康检查 | `http://192.168.48.104:48082/healthz` → `{"status":"ok"}` |

- 宿主端口 48082 映射到容器内 8082。
- 鉴权：当前 `MCP_API_KEY` 为空，**不需要** `Authorization` 头。若日后启用，调用方需带 `Authorization: Bearer <MCP_API_KEY>`，否则 `/mcp` 返回 401（`/healthz` 始终开放）。
- 服务只暴露 `/mcp` 和 `/healthz`；`/`、`/docs`、`/openapi.json` 均为 404。

## 2. 协议要点

- 传输：Streamable HTTP，单端点 `POST /mcp`，JSON-RPC 2.0。
- 服务端配置为**无状态 + JSON 响应**：无需先 `initialize` 建立会话，可省略 `Mcp-Session-Id`；响应体是普通 JSON（不是 SSE）。
- 建议请求头：`Content-Type: application/json` 和 `Accept: application/json, text/event-stream`，以兼容其他 MCP 客户端。
- 不做 Host / Origin 校验，跨站调用不受限。
- 请求体上限约 33.4 MiB（`4*((MAX_UPLOAD_BYTES+2)//3)+65536`），超出返回 413。

## 3. 工具一：`upload_attachment`（上传附件至 MinIO）

把文件内容上传到 MinIO，返回一个可直接下载的预签名 URL，专供 `hazard_analysis_expert` 的 `attachments` 使用。

### 入参

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `user_id` | 是 | 当前登录用户的真实 ID，1–256 字符、不含空白 |
| `filename` | 是 | 含扩展名、不含目录，≤255 字符，不能含 `/` `\` 或控制字符 |
| `content_base64` | 是 | 文件字节的**标准 Base64**，不带 `data:` 前缀 |
| `content_type` | 否 | MIME 类型；省略时按文件扩展名推断，推断不出用 `application/octet-stream` |

### 返回（`structuredContent`）

| 字段 | 说明 |
| --- | --- |
| `url` | 预签名下载地址，可直接传给专家工具 |
| `filename` | 原文件名 |
| `content_type` | 实际使用的 MIME 类型 |
| `size_bytes` | 解码后的字节数 |
| `bucket` | 桶名（当前为 `attachments`） |
| `object_key` | 对象键 |
| `expires_at` | URL 过期时间（ISO 8601，UTC） |

### 关键约束

- 单文件上限由 `MAX_UPLOAD_BYTES` 控制，当前 **25 MiB**；Base64 会放大 1/3，注意请求体总量。
- 对象键格式：`hazard-expert/<sha256(user_id)>/<YYYY/MM/DD>/<随机ID>-<filename>`，同一用户的不同上传永不覆盖。
- 预签名有效期由 `MINIO_PRESIGN_EXPIRY_SECONDS` 控制，当前 **86400 秒**（24 小时）。
- 上传使用 S3 API（path-style + SigV4），**不会**自动创建桶，桶必须已存在；凭证需具备目标前缀的 `PutObject`/`GetObject` 权限。
- 返回的 URL 用 `MINIO_DOWNLOAD_ENDPOINT` 签名（当前 `http://192.168.199.167:9000`），因此**下游 nanobot 必须能访问该地址**才能取到附件。

## 4. 工具二：`hazard_analysis_expert`（隐患识别与分析专家）

把用户问题、会话 ID 和附件 URL 转发给 container-up 的 `inbound-sync`，等待并返回最终答案。专家能力来自该 frontend 对应的 nanobot 配置、提示词与 Skill。

### 入参

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `user_id` | 是 | 必须是当前登录用户 ID，1–256 字符、不含空白 |
| `chat_id` | 是 | 会话 ID，1–256 字符、不含空白。**复用同一 `chat_id` 即同一会话多轮对话**；换新值即开启独立会话 |
| `query` | 是 | 用户问题，1–100000 字符 |
| `attachments` | 否 | 附件 http(s) URL 数组，最多 20 个；无附件可省略 |

### 返回（`structuredContent`）

| 字段 | 说明 |
| --- | --- |
| `status` | 固定为 `completed` |
| `user_id` / `chat_id` | 回显，用于校验 |
| `content` | 分析结果正文（Markdown，可能包含隐患清单、等级、整改措施及报告下载链接） |
| `attachments` | 后端回传的附件列表，通常为空 |

### 关键约束

- 上游地址：`http://192.168.199.168:30081/inbound-sync/web-test`（frontend `web-test`）。
- **同步长耗时**：实测一张现场照片约 385 秒；冷启动可能更久。客户端超时按 ≥720 秒设置（服务端 `CONTAINER_UP_TIMEOUT_SECONDS=720`）。
- **设计上不自动重试**：一次 POST 可能触发一次完整的 agent 任务，重复提交会重复执行。超时后应先查状态，不要盲目重发。
- 附件 URL 必须是 http(s)、不能带账号密码、不能是本地路径。
- 服务端会校验上游返回的 `user_id`、`chat_id`、`frontend_id` 与请求一致，不一致即报错。

## 5. 标准调用流程

先上传拿 URL，再把 URL 交给专家工具——**不要把 Base64 直接传给专家工具**。

```
1. upload_attachment  { user_id, filename, content_base64 }
       └─> url
2. hazard_analysis_expert { user_id, chat_id, query, attachments: [url] }
       └─> content（隐患清单 / 等级 / 整改措施 / 报告链接）
```

同一用户多轮对话：第二次起沿用相同 `user_id` + `chat_id`，`attachments` 按需增减。

## 6. 客户端接入

### nanobot（`config.json` 的 `mcpServers`）

```json
"hazard-expert-mcp": {
  "type": "streamableHttp",
  "url": "http://192.168.48.104:48082/mcp",
  "toolTimeout": 720,
  "enabledTools": ["*"]
}
```

`toolTimeout` 必须给足（20 分钟量级），否则长任务会在客户端侧被取消。`enabledTools` 可写 `["*"]` 或两个工具名。

### 通用 MCP 客户端

```json
{
  "mcpServers": {
    "hazard-expert": {
      "type": "http",
      "url": "http://192.168.48.104:48082/mcp"
    }
  }
}
```

启用鉴权后再补 `"headers": {"Authorization": "Bearer <MCP_API_KEY>"}`。

### curl

```bash
# 1) 上传
curl -s -X POST http://192.168.48.104:48082/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
        "name":"upload_attachment","arguments":{
          "user_id":"2021088","filename":"现场照片.jpg",
          "content_base64":"<Base64>","content_type":"image/jpeg"}}}'

# 2) 分析（超时留足）
curl -s -X POST http://192.168.48.104:48082/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{
        "name":"hazard_analysis_expert","arguments":{
          "user_id":"2021088","chat_id":"session-001",
          "query":"请识别现场隐患并提出整改措施。",
          "attachments":["<上一步返回的 url>"]}}}'
```

### Python（MCP SDK）

```python
import base64
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def inspect_site(mcp_url, user_id, chat_id, local_file):
    path = Path(local_file)
    async with streamablehttp_client(mcp_url, timeout=750, sse_read_timeout=750) as (read, write, _):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=750)) as session:
            await session.initialize()
            uploaded = await session.call_tool("upload_attachment", {
                "user_id": user_id,
                "filename": path.name,
                "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            })
            url = uploaded.structuredContent["url"]
            result = await session.call_tool("hazard_analysis_expert", {
                "user_id": user_id,
                "chat_id": chat_id,
                "query": "请识别现场隐患并提出整改措施。",
                "attachments": [url],
            })
            return result.structuredContent["content"]
```

## 7. 错误与排查

工具级错误通过 JSON-RPC 返回 `isError: true`，`content[0].text` 为原因：

| 提示 | 含义 |
| --- | --- |
| `附件超过 MAX_UPLOAD_BYTES 限制。` | 单文件超过 25 MiB |
| `content_base64 必须是标准 Base64，不包含 data: 前缀。` | 编码不合法，或带了 `data:image/jpeg;base64,` 前缀 |
| `filename 必须是文件名，不能包含目录或控制字符，最长 255 字符。` | 文件名带路径或非法字符 |
| `content_type 必须是有效的 MIME 类型。` | MIME 格式不合法 |
| `MinIO 上传失败，请检查连接、桶及读写权限。` | MinIO 不可达、桶不存在或凭证无权限 |
| `每次最多传入 20 个附件 URL。` | 附件数量超限 |
| `附件必须是 http(s) URL，不能是本地路径或包含账号密码。` | 附件地址不合法 |
| `等待隐患分析结果超时；后端可能仍在执行，请勿自动重复提交。` | 720 秒内未拿到结果 |
| `container-up 返回 HTTP <code>...` | 上游报错，需看服务端日志 |
| `container-up 连接异常；请求是否已执行未知...` | 网络层失败，结果未知 |
| `container-up 回复格式或用户/会话标识不匹配。` | 上游返回结构异常或标识对不上 |

其他：

- HTTP 401：启用了鉴权但未带/带错 token。
- HTTP 413：请求体超过约 33.4 MiB，通常是把过大文件塞进了单次上传。
- 日志：`docker logs -f hazard-expert-mcp-hazard-expert-mcp-1`。

## 8. 安全须知

- 当前实例**未启用鉴权**，任何能访问 48082 的客户端都能调用，并且可以传入任意 `user_id` 冒充他人。若该端口暴露范围超出可信内网，请在网关/防火墙层限制来源，或启用 `MCP_API_KEY`。
- `user_id` 是服务级透传，服务端不做身份校验；调用方必须绑定当前登录用户的真实 ID，不能允许终端用户自选。
- `hazard_analysis_expert` 会真实触发 nanobot 任务并产生报告文件，压测前请与控制方确认。

## 9. 实测样例（2026-10-16）

| 步骤 | 结果 |
| --- | --- |
| `upload_attachment` | 94,283 字节 JPG → 桶 `attachments`，`object_key` 前缀 `hazard-expert/138c170d.../2026/10/16/`，200 / `isError=false` |
| `hazard_analysis_expert` | `user_id=2021088`，带上图 URL，**385 秒**返回 200，`status=completed` |
| 分析结论 | 重大隐患 0 项、一般隐患 2 项（吊物下方站人、材料堆放凌乱），另附 20 项"需现场进一步核查"清单 |
| 报告 PDF | 464 KB、2 页，`http://192.168.199.167:9000/attachments/web-outbound/web-main/user/hazard_20261016_172500.pdf`，HTTP 200 |

纯文字提问（不带附件）同样可用：`query="你好"` 约 6 秒返回。