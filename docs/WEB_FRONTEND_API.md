# Web 前端对接 API

Base：`http://192.168.199.174:30080`

前端只需对接两个接口：发送用户消息、接收 AI 回复回调。

## 1. 发送消息

`POST http://192.168.199.174:30080/inbound/{frontend_id}`

前端提交用户消息。接口返回成功只代表消息已被后端接收，AI 回复不在此响应返回，会通过接口 2 回调给前端。

请求头：`Content-Type: application/json`

路径参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `frontend_id` | `string` | 是 | frontend 标识，固定使用 `web-wd` |

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | `string` | 是 | 用户唯一标识，二滩测试固定使用ettest |
| `chat_id` | `string` | 是 | 会话唯一标识，用于区分不同会话上下文 |
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

成功响应（HTTP 200）：

```json
{
  "status": "accepted",
  "frontend_id": "web-main",
  "user_id": "web-demo-1",
  "bucket_id": "bucket-0001",
  "instance_id": "web-demo-1"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | `string` | 固定为 `accepted` |
| `frontend_id` | `string` | frontend 标识 |
| `user_id` | `string` | 用户标识 |
| `bucket_id` | `string` | 后端处理实例所在 bucket |
| `instance_id` | `string` | 后端处理实例 ID |

失败响应：

```json
{
  "detail": "..."
}
```

## 2. 接收 AI 回复（回调）

前端需提供一个内网可访问的 HTTP 接口，后端会向该地址发起 `POST` 请求推送 AI 回复。回调接口开发完成后联系后端进行配置

`POST {前端提供的回调地址}`

请求头：`Content-Type: application/json`

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `frontend_id` | `string` | 是 | frontend 标识，固定为web-wd |
| `user_id` | `string` | 是 | 用户唯一标识，与发送消息时一致，二滩测试应为ettest |
| `chat_id` | `string` | 是 | 会话唯一标识，与发送消息时一致 |
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

前端需返回（HTTP 200）：

```json
{
  "status": "accepted"
}
```

## 3. 字段约定

`attachments`：数组，元素为成都院内网可访问的对象 URL 链接，可上传至minio服务获取内网链接，参考（4、文件上传），例如：

```json
[
  "https://example.com/files/demo.png"
]
```

`metadata`：无固定 schema，前端放业务自定义字段即可，后端回调时可能保留并补充额外字段，请对未知字段保持兼容。

```json
{
  "trace_id": "trace-001",
  "client_msg_id": "msg-001",
  "source": "frontend"
}
```

## 4. 文件上传（MinIO）

附件上传到 MinIO 后以 URL 形式放入 `attachments`：

```json
{
  "endpoint": "http://192.168.199.167:9000",
  "access_key": "<MINIO_ACCESS_KEY>",
  "secret_key": "<MINIO_SECRET_KEY>",
  "bucket": "attachments"
}
```
