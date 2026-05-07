# Web Frontend HTTP API

本文只描述前端直接对接 `container_up` 时需要使用的两个 HTTP 接口：

- `POST /inbound/{frontend_id}`
- `POST /outbound`

不涉及内部实现。

## 1. 接口说明

### 1.1 `POST /inbound/{frontend_id}`

用途：
前端直接向 `container_up` 提交用户消息。

请求头：

```text
Content-Type: application/json
```

路径参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `frontend_id` | `string` | 是 | frontend 标识，当前固定使用 `web-main` |

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | `string` | 是 | 用户唯一标识 |
| `chat_id` | `string` | 是 | 会话唯一标识 |
| `content` | `string` | 是 | 用户输入文本；无文本时可传空字符串 |
| `attachments` | `array` | 否 | 附件列表，默认 `[]` |
| `metadata` | `object` | 否 | 业务扩展字段，默认 `{}` |
| `raw` | `object` | 否 | 原始上下文字段，默认 `{}` |

请求示例：

```json
{
  "user_id": "web-demo-1",
  "chat_id": "web-chat-1",
  "content": "hello from web frontend",
  "attachments": [],
  "metadata": {},
  "raw": {}
}
```

成功响应示例：

```json
{
  "status": "accepted",
  "frontend_id": "web-main",
  "user_id": "web-demo-1",
  "bucket_id": "bucket-0001",
  "instance_id": "web-demo-1"
}
```

成功响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | `string` | 固定为 `accepted` |
| `frontend_id` | `string` | frontend 标识 |
| `user_id` | `string` | 用户标识 |
| `bucket_id` | `string` | 后端处理实例所在 bucket |
| `instance_id` | `string` | 后端处理实例 ID |

失败响应示例：

```json
{
  "detail": "..."
}
```

说明：

- `POST /inbound` 成功只表示消息已被后端接收。
- 真实请求路径示例：`POST /inbound/web-main`
- AI 回复会通过 `POST /outbound` 回调下发，不会直接出现在 `POST /inbound` 的响应里。

### 1.2 `POST /outbound`

用途：
`container_up` 将 AI 回复回调给前端后端。

请求头：

```text
Content-Type: application/json
```

请求体：

| 字段 | 类型 | 必有 | 说明 |
| --- | --- | --- | --- |
| `frontend_id` | `string` | 是 | frontend 标识 |
| `user_id` | `string` | 是 | 用户唯一标识 |
| `chat_id` | `string` | 是 | 会话唯一标识 |
| `content` | `string` | 是 | AI 回复文本；纯附件回复时可能为空字符串 |
| `attachments` | `array` | 是 | 回复附件列表，默认 `[]` |
| `metadata` | `object` | 是 | 透传业务字段和扩展字段，默认 `{}` |

纯文本回调示例：

```json
{
  "frontend_id": "web-main",
  "user_id": "web-demo-1",
  "chat_id": "web-chat-1",
  "content": "Hello! Connection received from the frontend.",
  "attachments": [],
  "metadata": {
    "frontend_id": "web-main",
    "usr_id": "web-demo-1"
  }
}
```

带附件回调示例：

```json
{
  "frontend_id": "web-main",
  "user_id": "web-demo-1",
  "chat_id": "web-chat-1",
  "content": "处理完成，请查看附件。",
  "attachments": [
    {
      "url": "https://example.com/files/report.pdf",
      "filename": "report.pdf",
      "content_type": "application/pdf"
    }
  ],
  "metadata": {
    "frontend_id": "web-main",
    "usr_id": "web-demo-1"
  }
}
```

前端后端收到回调后的成功响应建议：

```json
{
  "status": "accepted"
}
```

要求：

- HTTP 状态码返回 `200`
- 返回体建议为 `{"status":"accepted"}`

## 2. attachments 字段约定

`attachments` 建议按以下两种格式处理。

字符串格式：

```json
[
  "https://example.com/files/demo.png"
]
```

对象格式：

```json
[
  {
    "url": "https://example.com/files/demo.png",
    "filename": "demo.png",
    "content_type": "image/png"
  }
]
```

前端联调建议：

- 入站先用 `attachments: []` 做文本联调
- 如需做附件联调，优先使用对象格式
- 出站处理时同时兼容字符串格式和对象格式

## 3. metadata 字段约定

`metadata` 没有固定强制 schema，建议前端只放业务自定义字段，例如：

```json
{
  "trace_id": "trace-001",
  "client_msg_id": "msg-001",
  "source": "frontend"
}
```

后端回调时，`metadata` 可能会保留这些字段，并补充额外字段。前端后端应对未知字段保持兼容。

## 4. 最小联调样例

入站请求：

```bash
curl -sS -X POST http://127.0.0.1:30080/inbound/web-main \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id":"web-demo-1",
    "chat_id":"web-chat-1",
    "content":"hello from web frontend",
    "attachments":[],
    "metadata":{},
    "raw":{}
  }'
```

出站接收成功响应：

```json
{
  "status": "accepted"
}
```

## 5. 联调要求

- `user_id` 由前端保证稳定，不要同一用户频繁变更
- `chat_id` 由前端保证唯一，用于区分不同会话
- `POST /outbound` 必须可被 `container_up` 访问
- `POST /outbound` 收到回调后应尽快返回 `200`
