# Web Frontend HTTP API

本文描述 Web 前端对接 `container_up` 时的 HTTP 交互约定，分为两部分：

- 前端调用 `container_up` 的入站接口：`POST /inbound/{frontend_id}`
- 前端自行实现的消息回调接口：用于接收 `container_up` 推送的回复消息

不涉及内部实现。

## 1. 接口说明

### 1.1 `POST http://192.168.48.104/inbound/{frontend_id}`

用途：
前端直接向 `container_up` 提交用户消息。

接口提供方：
`container_up` 后端。

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
| `user_id` | `string` | 是 | 用户唯一标识，优先使用工号/电话号码等具有实际含义的id |
| `chat_id` | `string` | 是 | 会话唯一标识，用于前端控制后端上下文切换 |
| `content` | `string` | 是 | 用户输入文本 |
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
- 回复不会直接出现在 `POST /inbound` 的响应里。
- 回复会由 `container_up` 主动回调到前端提供的消息接收接口。

### 1.2 前端消息回调接口

用途：
前端提供一个可被 `container_up` 访问的 HTTP 接口，用于接收 AI 回复消息。

接口提供方：
前端。

说明：

- `container_up` 会按配置中的回调地址向该接口发起 `POST` 请求。
- 回调地址要内网可访问。
- 本文只定义 `container_up` 发起回调时的请求字段和前端应返回的响应约定。

请求头：

```text
Content-Type: application/json
```

请求体：

| 字段 | 类型 | 必有 | 说明 |
| --- | --- | --- | --- |
| `frontend_id` | `string` | 是 | frontend 标识，以前后端实际约定为准 |
| `user_id` | `string` | 是 | 用户唯一标识，与inbound请求中的id一致 |
| `chat_id` | `string` | 是 | 会话唯一标识，与inbound请求中的id一致 |
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

前端收到回调后的成功响应建议：

```json
{
  "status": "accepted"
}
```

要求：

- HTTP 状态码返回 `200`
- 返回体建议为 `{"status":"accepted"}`

## 2. attachments 字段约定

`attachments` 建议上传可访问的对象url链接。

字符串格式：

```json
[
  "https://example.com/files/demo.png"
]
```

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
curl -sS -X POST http://192.168.48.104:30080/inbound/web-main \
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

## 5. minio配置
```bash
{
    "endpoint": "http://192.168.48.104:9000",
    "access_key": "minio_admin",
    "secret_key": "minio_password",
    "bucket": "attachments"
}
```

## 6. 其他

- 'frontend_id'以前后端实际约定为准
- `user_id` 由前端保证稳定，不要同一用户频繁变更
- `chat_id` 由前端保证唯一，用于区分不同会话
- 前端提供的消息回调 URL 必须可被 `container_up` 访问
- 前端回调接口收到消息后应尽快返回 `200
- 前端回调接口确认后告知后端进行配置
