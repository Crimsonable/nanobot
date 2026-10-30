# Web 前端同步请求 API

Base：`http://<节点地址>:30081`。参考环境为 `http://192.168.199.168:30081`，实际以部署为准。

集群内地址：`http://channel-gateway.opencode-sv.svc.cluster.local:8000`。

前端发送消息后，在同一个 HTTP 响应中接收 AI 最终回复，无需提供回调接口。超时后可查询或取消原请求。本文使用仓库配置中的 `web-test`，接入时应使用后端已注册的 frontend 标识。

## 1. 发送消息并等待 AI 回复

`POST {Base}/inbound-sync/{frontend_id}`

请求头：`Content-Type: application/json`。当前 Gateway 不要求 API key 或 `Authorization` 请求头。

路径参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `frontend_id` | `string` | 是 | 接入渠道标识，用于选择后端模型、运行环境及附件存储配置。示例为 `web-test`，必须已在后端注册 |

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | `string` | 是 | 用户唯一标识，用于区分身份及用户工作目录。同一用户保持不变，长度 1～500 |
| `chat_id` | `string` | 否 | 会话标识，在同一 `frontend_id + user_id` 下区分会话。相同值继续原会话，新值开启另一会话。省略、`null` 或纯空白时使用 `default`，建议显式填写 |
| `request_id` | `string` | 否 | 单条消息的唯一编号，用于重试去重、查询及取消。建议前端生成全局唯一 UUID，非空时最长 100 字符；省略、`null` 或空字符串时后端生成。每条新消息使用新编号 |
| `content` | `string` | 是 | 用户文本或附件处理要求，最长 200000 字符。仅传附件时也须提供，可填空字符串 |
| `attachments` | `array` | 否 | 附件引用列表，默认 `[]`，最多 32 个。支持 URL 字符串或附件对象，详见第 4 节；不传文件二进制或 Base64 |
| `metadata` | `object` | 否 | 业务扩展信息，例如流水号、页面来源。默认 `{}`，随请求记录并在响应中返回，不用于选择会话 |
| `stream` | `boolean` | 否 | 默认 `false`，等待完成后返回 JSON；设为 `true` 时在同一响应中接收原生事件 SSE，见第 7 节。仅接受 JSON 布尔值 |

未知请求字段会被忽略。参考异步文档中的 `raw` 不是本接口支持的业务字段，需要保留的信息放入 `metadata`。

请求示例：

```json
{
  "user_id": "web-demo-1",
  "chat_id": "web-chat-1",
  "request_id": "web-msg-20261028-001",
  "content": "分析附件并生成报告",
  "attachments": [
    {
      "url": "http://192.168.199.167:9000/attachments-test/uploads/report.pdf",
      "filename": "report.pdf",
      "content_type": "application/pdf"
    }
  ],
  "metadata": {"trace_id": "trace-001", "source": "frontend"}
}
```

示例附件地址仅展示格式，请替换成上传后可访问的 URL；私有对象使用完整预签名下载 URL。

成功响应（HTTP 200，示意）：

```json
{
  "request_id": "web-msg-20261028-001",
  "status": "completed",
  "frontend_id": "web-test",
  "user_id": "web-demo-1",
  "chat_id": "web-chat-1",
  "bucket_id": "opencode-bucket-012345abcdef",
  "identity_id": "<后端生成的身份哈希>",
  "content": "附件分析完成。",
  "attachments": [],
  "metadata": {"trace_id": "trace-001", "source": "frontend"},
  "delivery_state": "pending",
  "error": null,
  "partial": false,
  "usage": null
}
```

响应字段（查询、取消接口也使用此结构）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `request_id` | `string` | 本次请求编号，保存它以便查询或取消 |
| `status` | `string` | 执行状态：`completed` 完成、`failed` 失败、`cancelled` 已取消；查询时还可能出现 `queued` 等非终态 |
| `frontend_id` | `string` | 本次请求所属的渠道标识 |
| `user_id` | `string` | 本次请求所属的用户标识 |
| `chat_id` | `string` | 本次请求所属的会话标识，缺省时为 `default` |
| `bucket_id` | `string` | 后端运行容器分组 ID，供排查使用，尚未分配时可能为空；不是 MinIO 存储桶名称 |
| `identity_id` | `string` | 后端根据 `frontend_id + user_id` 生成的身份哈希，前端无需生成 |
| `content` | `string` | AI 最终回复文本，纯附件回复时可能为空；结果尚未到达时没有此字段 |
| `attachments` | `array<object>` | 回复附件列表，默认 `[]`，使用对象中的 `url` 下载；结果尚未到达时没有此字段 |
| `metadata` | `object` | 提交时的业务扩展信息，对未知业务字段保持兼容 |
| `delivery_state` | `string` | 渠道回调投递状态。同步请求不发送回调，通常保持 `pending`，不表示 AI 仍在执行。异步请求还可能为 `sending`、`sent`、`unknown`、`skipped` |
| `error` | `string / null` | 错误说明，无错误时为 `null`；结果到达前可能没有此字段 |
| `partial` | `boolean` | 是否为不完整结果，默认 `false`；为 `true` 时不能把正文当作完整成果。结果到达前可能没有此字段 |
| `usage` | `object / null` | 模型 token 用量，无统计时为 `null`；结果到达前可能没有此字段 |

`usage` 非空时的字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `input_tokens` | `integer` | 输入 token 数，默认 0 |
| `output_tokens` | `integer` | 输出 token 数，默认 0 |
| `reasoning_tokens` | `integer` | 推理 token 数，默认 0 |
| `cache_read_tokens` | `integer` | 从缓存读取的 token 数，默认 0 |
| `cache_write_tokens` | `integer` | 写入缓存的 token 数，默认 0 |

HTTP 200 只表示接口正常返回，前端仍须检查 `status`。例如 `status` 为 `failed` 时展示 `error`，不能按执行成功处理。

## 2. 超时与查询请求

同步等待上限由后端 `control.limits.request_seconds` 控制，默认 600 秒。前端客户端和反向代理的读取超时应大于该值。

等待超时响应（HTTP 504）：

```json
{
  "detail": {
    "request_id": "web-msg-20261028-001",
    "error": "reply timeout"
  }
}
```

超时不代表任务已经停止，使用原编号查询：

`GET {Base}/requests/{frontend_id}/{request_id}`

| 路径参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `frontend_id` | `string` | 是 | 与发送消息时一致的渠道标识 |
| `request_id` | `string` | 是 | 发送时指定或后端返回的请求编号 |

无请求体、无查询参数。

请求示例：

```http
GET /requests/web-test/web-msg-20261028-001
```

成功响应为 HTTP 200，字段见第 1 节。未收到最终结果时，不含 `content`、`attachments`、`error`、`partial`、`usage`；继续查询，直到状态为 `completed`、`failed` 或 `cancelled`。请求不存在或不属于该 frontend 时返回 HTTP 404。

重试约定：相同 `request_id`、相同 frontend、相同请求内容及相同调用方式会复用原请求；修改内容后复用编号返回 HTTP 409。不要因超时换一个编号盲目重发有副作用的任务。

## 3. 取消请求

`POST {Base}/requests/{frontend_id}/{request_id}/cancel`

| 路径参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `frontend_id` | `string` | 是 | 与发送消息时一致的渠道标识 |
| `request_id` | `string` | 是 | 要取消的请求编号 |

无请求体、无查询参数。

请求示例：

```http
POST /requests/web-test/web-msg-20261028-001/cancel
```

成功响应为 HTTP 200，返回当前请求状态，字段见第 1 节。成功代表取消请求已记录，不保证此刻已经停止，应继续查询确认最终状态。已经完成的请求不会因取消而撤销其结果。

## 4. 附件字段约定

### 4.1 发送附件

先上传至 MinIO，再把下载 URL 放入 `attachments`。Gateway 只接收引用，不负责上传或下载文件，也没有独立附件上传接口。

简单格式：

```json
[
  "http://192.168.199.167:9000/attachments-test/uploads/demo.png"
]
```

指定文件名或类型时使用对象，示例见第 1 节：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `url` | `string` | 是（Web 接入） | 后端可访问的下载地址。私有对象使用预签名 GET 链接，完整保留签名查询参数 |
| `filename` | `string` | 否 | 文件名，例如 `report.pdf`。省略时从 URL 路径推断，无法推断时为 `attachment.bin`；不能包含目录分隔符 |
| `content_type` | `string` | 否 | MIME 类型，例如 `application/pdf`、`image/png`。省略时从文件名推断，无法推断时为 `application/octet-stream` |
| `requires_auth` | `boolean` | 否 | 是否需要额外鉴权的标记，默认 `false`；不会代为登录或附加凭据。预签名链接通常填 `false` |
| `path` | `string` | 否 | 兼容内部调用的文件路径引用，与 `url` 同时提供时优先使用 `url`。浏览器本地路径不能供后端访问，Web 接入请上传后传 URL |

### 4.2 接收附件

AI 生成文件由后端 `publish_attachment` 上传到该 frontend 配置的 MinIO。前端用 `filename` 展示、用 `url` 下载。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `attachment_id` | `string` | 后端生成的附件唯一编号 |
| `url` | `string` | HTTP/HTTPS 下载地址；签名链接需在过期前使用 |
| `filename` | `string` | 文件名，例如 `report.pdf` |
| `content_type` | `string` | 文件 MIME 类型 |
| `size` | `integer` | 文件大小，单位字节，默认 0 |
| `sha256` | `string` | 文件内容的 SHA-256 校验值，未提供时为空字符串 |
| `storage` | `string` | 存储类型，发布至 MinIO 时为 `minio` |
| `object_key` | `string` | 文件在 MinIO bucket 内的对象路径，供排查使用 |
| `path` | `string` | 后端文件路径信息，可能为空；前端下载使用 `url` |
| `requires_auth` | `boolean` | 附件鉴权标记，默认 `false`，不是访问凭据 |

## 5. 文件上传与 MinIO 配置

### 5.1 前端业务服务上传配置

以下为参考文档中的内网测试配置，凭据与仓库 MinIO 部署清单一致；实际可达性及存储桶是否已创建以部署环境为准。

```json
{
  "endpoint": "http://192.168.199.167:9000",
  "access_key": "minio_admin_jfhtkw8kds",
  "secret_key": "minio_password_asdfjhur8k2kd",
  "bucket": "attachments-test"
}
```

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `endpoint` | `string` | 是 | MinIO S3 API 地址，上传及生成签名使用 9000 端口；9001 是管理控制台端口 |
| `access_key` | `string` | 是 | 上传服务使用的 MinIO 账号/访问密钥 ID |
| `secret_key` | `string` | 是 | 配套签名密钥，用于认证上传及生成预签名 URL |
| `bucket` | `string` | 是 | 附件存储桶名称，此处为 `attachments-test`，上传前须创建并授予访问权限 |

凭据放在业务服务端配置中，不写入浏览器前端代码。浏览器可通过业务服务上传，或由业务服务签发临时上传 URL 后直传；浏览器直传时需配置 MinIO CORS。

上传步骤：

1. 业务服务通过 MinIO/S3 SDK 上传到 `attachments-test`，使用唯一对象路径，例如 `uploads/<唯一编号>/report.pdf`，避免同名覆盖。
2. 获取下载 URL。对象允许匿名读取时可使用 `http://192.168.199.167:9000/attachments-test/<对象路径>`；私有对象由业务服务生成预签名 GET URL。
3. 将下载 URL 放入 `attachments`。上传用的预签名 PUT URL 不能作为下载 URL 使用。
4. 确保后端可访问 URL，签名有效期覆盖排队及处理时间。回复附件的下载地址还须对用户浏览器可达。

### 5.2 后端发布 AI 附件的配置

在 `config/opencode/frontends.json` 对应 frontend 的 `attachment_storage` 中配置。以下是该字段示例，不是整个配置文件：

```json
{
  "attachment_storage": {
    "provider": "minio",
    "endpoint": "http://192.168.199.167:9000",
    "access_key": "minio_admin_jfhtkw8kds",
    "secret_key": "minio_password_asdfjhur8k2kd",
    "bucket": "attachments-test",
    "region": "us-east-1",
    "public_base_url": "",
    "key_prefix": "web-outbound",
    "presign_expiry_seconds": 3600
  }
}
```

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `provider` | `string` | 否 | 存储类型，固定为 `minio`，默认 `minio` |
| `endpoint` | `string` | 是 | 后端访问 MinIO 的 S3 API 地址。签名下载链接也基于此地址生成，因此签名模式下浏览器也须可访问 |
| `bucket` | `string` | 是 | AI 文件上传到的存储桶。示例为 `attachments-test`；仓库原始模板为 `attachments`，接入时明确选择已创建的桶 |
| `access_key` | `string` | 二选一 | 直接提供访问密钥 ID，与 `access_key_file` 必须且只能配置一个 |
| `access_key_file` | `string` | 二选一 | 保存访问密钥 ID 的后端文件路径，替代 `access_key`，例如 `/mnt/common/web-test/config/minio-access-key` |
| `secret_key` | `string` | 二选一 | 直接提供签名密钥，与 `secret_key_file` 必须且只能配置一个 |
| `secret_key_file` | `string` | 二选一 | 保存签名密钥的后端文件路径，替代 `secret_key`，例如 `/mnt/common/web-test/config/minio-secret-key`。凭据文件须位于该 frontend 的配置目录内 |
| `region` | `string` | 否 | S3 签名区域，默认 `us-east-1` |
| `public_base_url` | `string` | 否 | 公开下载地址前缀，默认空字符串。非空时后端拼接此前缀和对象路径，不生成签名。例如 `http://192.168.199.167:9000/attachments-test`，须包含 bucket 且已有公开读取权限；配置此值不会自动开放桶权限 |
| `key_prefix` | `string` | 否 | 上传对象路径前缀，默认 `attachments`，示例为 `web-outbound`；后端还会追加身份哈希和文件标识等路径 |
| `presign_expiry_seconds` | `integer` | 否 | 签名下载链接有效期，单位秒，默认 86400，配置校验最小 60；示例 3600 表示 1 小时。仅 `public_base_url` 为空时生效 |

集群内部可用 `http://minio.minio.svc.cluster.local:9000` 作为 endpoint，但浏览器通常无法解析此域名。用该地址上传时，应配合可访问且已有读取权限的 `public_base_url`；签名模式使用内外都可达的 endpoint，不要生成签名后自行替换 URL 主机名。

## 6. 常见失败响应与对接注意事项

一般失败响应：

```json
{
  "detail": "request_id already used for different input"
}
```

| HTTP 状态码 | 含义 | 前端处理 |
| --- | --- | --- |
| `400` | 业务参数校验失败，例如附件缺少 URL 或路径 | 根据 `detail` 修正参数 |
| `404` | frontend 未注册，或请求不存在、不属于该 frontend | 核对渠道标识和请求编号 |
| `409` | 编号用于不同内容，或请求被后端明确拒绝 | 查看 `detail`；新消息用新编号，原请求重试保持内容一致 |
| `422` | 请求格式、必填字段、类型或数量限制不满足要求 | `detail` 通常是校验错误数组，按字段修正 |
| `503` | 后端控制服务暂时不可用 | 已接收请求可能仍在队列，保留原编号查询或重试 |
| `504` | 等待回复超时 | 使用 `detail.request_id` 查询或取消 |

`user_id` 是用户标识，`chat_id` 是业务会话标识，`request_id` 是单条消息编号。OpenCode 内部 `session_id` 由后端维护，前端不需要填写；详见 [日志与 ID 约定](SESSION_LOGGING.md)。

异步接口 `POST /inbound/{frontend_id}` 立即返回请求状态，最终通过渠道/回调发送回复；同步与异步调用不能共用同一 `request_id`。可选 `web_server` 的 `/inbound` 是异步代理，同步请求直接调用 Gateway。同步模式无需业务回调；异步接入的 `send_msg_url` 用于指定回复回调地址。

附件链路说明见 [附件协议](ATTACHMENT_UPLOAD_API.md)。

## 7. 流式接收 OpenCode 原生事件

仅 Web 渠道的 `POST /inbound-sync/{frontend_id}` 支持 `stream: true`。接口仍是 POST，同步请求不触发渠道回调。省略 `stream` 与显式 `false` 完全等价，保持原有 JSON 响应和请求去重行为；`true` 属于不同的请求指纹，不能用已经提交的非流式请求编号切换模式。异步 `/inbound` 或非 Web 渠道传入 `stream: true` 返回 400。

请求示例：

```json
{
  "user_id": "web-demo-1",
  "chat_id": "web-chat-1",
  "request_id": "web-stream-001",
  "content": "分析这个项目",
  "stream": true
}
```

接受请求后返回 HTTP 200，响应头如下：

```http
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
X-Accel-Buffering: no
```

### 7.1 事件格式

每个事件由空行结束。`id` 是 Gateway 为本请求持久化的递增整数序号，从 1 开始；不是 OpenCode 事件 ID。`event` 区分网关控制事件与原生事件，`data` 是单行 JSON，正文中的换行按 JSON 转义。

| `event` | `data` | 行为 |
| --- | --- | --- |
| `gateway.start` | `version: 1`、`request_id`、`frontend_id`、`user_id`、`chat_id` | 本请求事件日志的第一条；不表示模型已开始执行 |
| `opencode.event` | 原生 OpenCode 事件对象，包含原始 `type`、`properties` 及其他字段 | 原样保留 JSON 内容，不转换成 AI SDK 消息格式，也不额外套业务包装 |
| `gateway.result` | 第 1 节定义的完整同步响应对象 | 最后一条持久化事件，`status` 为 `completed/failed/cancelled`，发送后关闭流 |
| `gateway.error` | `request_id`、`code`、`message` | 本次订阅失败，发送后关闭流；没有 `id`，不进入持久化事件序列，不代表执行已终止 |

示例（最终响应仅展示部分字段，实际返回第 1 节的完整结构）：

```text
id: 1
event: gateway.start
data: {"version":1,"request_id":"web-stream-001","frontend_id":"web-test","user_id":"web-demo-1","chat_id":"web-chat-1"}

id: 2
event: opencode.event
data: {"type":"message.part.updated","properties":{"part":{"id":"part_1","sessionID":"ses_1","messageID":"msg_1","type":"text","text":""}}}

id: 3
event: opencode.event
data: {"type":"message.part.delta","properties":{"sessionID":"ses_1","messageID":"msg_1","partID":"part_1","field":"text","delta":"分析完成。"}}

id: 4
event: gateway.result
data: {"request_id":"web-stream-001","status":"completed","content":"分析完成。","attachments":[],"error":null,"partial":false,"usage":null}

```

`gateway.result.content` 是最终快照，不能追加到已经流式显示的正文，否则会重复显示。不发送 `[DONE]`。未收到 `gateway.result` 就断开连接，不代表请求成功完成。客户端若已保存终态事件的序号，重连可以收到空的 SSE 响应并立即结束。

### 7.2 原生事件范围

BU 对流式请求绕过 `event_forwarding` 的事件类型、part 类型和子会话过滤，转发当前归属逻辑能够关联到本请求的所有原生事件，包括 reasoning、工具、文件、消息删除、未知类型及子会话事件。请求身份及工作目录校验仍然执行，不广播其他用户或其他请求的事件；无法关联的事件不猜测归属。普通请求继续使用原过滤规则。

流以 BU 有序发送的 `runtime.result` 为终态边界：Gateway 先发送已经持久化的前序原生事件，再发送整理后的 `gateway.result`。终态之后迟到的原生事件不会追加到已结束的 SSE 日志。不能把一条消息完成、工具完成或子会话 idle 当成整个请求完成。

这里的“原样”指 JSON 内容不改写，不保证原始 SSE 字节、JSON 空格和字段顺序不变。OpenCode 全局心跳不转发；Gateway 空闲时约每 15 秒发送 `: ping` 注释保活，注释没有序号，前端忽略即可。

BU → Gateway 仍使用可靠 WebSocket。大事件在 Gateway 完整重组后才输出一个 SSE 事件。Gateway 已接收的事件可以重放；OpenCode → BU 的 SSE 断线仍可能丢事件，新增出口不补齐该段缺失。

### 7.3 断线续传和去重

用**相同请求体、相同 `request_id`** 再次 POST，并携带最后一条已处理事件的序号：

```http
POST /inbound-sync/web-test
Content-Type: application/json
Last-Event-ID: 3
```

请求体仍须包含 `stream: true`。服务端从 `id > 3` 的事件开始重放，随后继续读取实时事件，不重新执行任务。不带 `Last-Event-ID` 或传 `0` 时从第一条开始重放；携带该头时必须显式提供已有请求编号。非法、负数或超过本请求现有最大序号的游标返回 400；续传请求不存在返回 404；相同编号但请求内容、身份或模式不同返回 409。

事件序号只在一个请求内有意义。前端按 `(request_id, id)` 去重，处理完事件再更新游标。网关重启后事件及终态仍可重放；事件日志与请求记录一同保存在 Gateway 状态目录，本版不增加自动过期清理。

浏览器使用 `fetch` POST 并解析 `response.body` 中的 SSE 帧；原生 `EventSource` 不支持携带 POST 请求体。网络 chunk 不等于一条 SSE 事件，解析器必须处理跨 chunk 的 UTF-8、行和空行边界。重连由客户端使用保存的请求及游标发起，不会自动进行。

### 7.4 失败、取消和等待上限

SSE 开始前的错误仍使用第 6 节的 HTTP 错误。开始发送后不能再改 HTTP 状态码：执行完成、失败或取消通过 `gateway.result.status` 表达；订阅错误通过 `gateway.error` 表达。

| `gateway.error.code` | 含义 |
| --- | --- |
| `STREAM_WAIT_TIMEOUT` | 当前订阅等待达到 `control.limits.request_seconds`；查询或续传原请求 |
| `REQUEST_REJECTED` | 已入队请求在后台恢复提交时被明确拒绝 |
| `STREAM_ERROR` | 事件流读取出现异常；查询或续传原请求 |

等待上限按每次 HTTP 订阅计算，重连不重置 BU 的执行截止时间。客户端断开只移除订阅，不自动取消任务；停止执行仍调用第 3 节的取消接口。反向代理应关闭响应缓冲，并允许长连接；读取超时应覆盖心跳间隔。

此协议不是 `useChatRuntime` 默认接受的 UI Message Stream，assistant-ui 前端需要原生事件适配层。Gateway 不维护用于 UI 转换的 text/reasoning/tool 状态机。

上线时同步更新 Gateway、CU 和 BU，确保 `stream` 标记能通过内部提交协议传到 BU；旧 BU 不支持该字段。
