# Web 前端对接 API（inbound-sync）

Base：`http://192.168.199.168:30081`

前端发送消息后，AI 回复直接在本次 HTTP 响应中返回，无需提供回调接口。

## 1. 发送消息

`POST http://192.168.199.168:30081/inbound-sync/web-stream`

请求头：`Content-Type: application/json`

路径参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `frontend_id` | `string` | 是 | frontend 标识，固定使用 `web-stream` |

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | `string` | 是 | 用户标识，测试固定使用 `streamtest` |
| `chat_id` | `string` | 否，建议传入 | 会话标识，省略时为 `default`；相同 ID 继续同一会话，更换 ID 开始新会话 |
| `content` | `string` | 是 | 用户输入文本 |
| `attachments` | `array` | 否 | 附件 URL 列表，默认 `[]` |
| `metadata` | `object` | 否 | 业务扩展字段，默认 `{}` |
| `raw` | `object` | 否 | 原始上下文字段，默认 `{}` |

可直接粘贴到 Bash 执行：

```bash
curl -sS -X POST 'http://192.168.199.168:30081/inbound-sync/web-stream' \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "user_id": "streamtest",
  "chat_id": "stream-chat-001",
  "content": "你好",
  "attachments": [],
  "metadata": {},
  "raw": {}
}
JSON
```

## 2. 接收 AI 回复（同步响应）

成功响应示例（HTTP 200）：

```json
{
  "status": "completed",
  "frontend_id": "web-stream",
  "user_id": "streamtest",
  "chat_id": "stream-chat-001",
  "bucket_id": "bucket-1",
  "instance_id": "web-stream__streamtest",
  "content": "你好，请问有什么可以帮助你？",
  "attachments": [],
  "metadata": {}
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | `string` | 成功时固定为 `completed` |
| `frontend_id` | `string` | frontend 标识，固定为 `web-stream` |
| `user_id` | `string` | 与请求的用户标识一致，为 `streamtest` |
| `chat_id` | `string` | 与请求的会话标识一致 |
| `bucket_id` | `string` | 后端处理实例所在 bucket，以实际返回为准 |
| `instance_id` | `string` | 后端处理实例 ID，以实际返回为准 |
| `content` | `string` | AI 回复文本；纯附件回复时可能为空字符串 |
| `attachments` | `array` | 回复附件列表，无附件时为 `[]` |
| `metadata` | `object` | 业务及扩展字段，请兼容未知字段 |

失败响应示例：

```json
{
  "detail": "错误说明"
}
```

## 3. 附件与扩展字段

请求中的 `attachments` 为后端可访问的文件 URL 数组。先上传文件到 MinIO，再将取得的下载链接填入数组，例如：

```json
[
  "http://192.168.199.167:9000/attachments/demo.png"
]
```

回复中的附件通常包含下载地址、文件名和类型，例如：

```json
[
  {
    "url": "http://192.168.199.167:9000/attachments/report.pdf",
    "filename": "report.pdf",
    "content_type": "application/pdf"
  }
]
```

示例文件地址需替换为实际下载链接；如链接带签名参数，请完整保留。回复附件也可能直接以 URL 字符串返回。

`metadata` 可放业务自定义字段，例如 `trace_id`、`client_msg_id`；不需要时传 `{}` 即可。

## 4. 文件上传（MinIO）

MinIO 配置沿用原有配置，账号和密钥不变：

```json
{
  "endpoint": "http://192.168.199.167:9000",
  "access_key": "minio_admin_jfhtkw8kds",
  "secret_key": "minio_password_asdfjhur8k2kd",
  "bucket": "attachments"
}
```
