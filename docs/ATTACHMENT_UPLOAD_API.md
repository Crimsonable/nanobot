# 内部附件上传与渠道分流

`POST /internal/attachments/upload` 位于 container-up，由 nanobot 的 `message` 工具调用。
本地文件由 nanobot 读取后以 multipart 文件内容上传，container-up 不需要读取用户工作目录。

| 字段 | 说明 |
| --- | --- |
| `frontend_id` | 必填，在服务端根据该 frontend 的配置确定渠道 |
| `user_id` | 当前用户 ID |
| `purpose` | `outbound` 表示渠道发送；省略或传 `reference` 表示获取供模型或 HTTP 调用方使用的下载 URL |
| `file` | multipart 请求中的文件内容 |
| `attachment_url` | 已有 URL 时使用 JSON 请求传入，与 multipart 文件上传二选一 |

接口按用途和渠道处理：

| 用途 | 渠道 | 处理结果 |
| --- | --- | --- |
| `outbound` | QXT | 文件内容直接上传 QXT `/v2/media/upload`，返回 `media_id`；已有 URL 则下载后上传 QXT |
| `outbound` | 其他渠道 | 本地文件上传 MinIO 返回 URL；已有 URL 保持原有规范化逻辑 |
| `reference` | 所有渠道 | 保持原有 URL 处理，本地文件上传 MinIO；用于入站附件解析及 `inbound-sync` 回复 |

QXT 上传结果示例：

```json
{
  "storage": "qxt",
  "frontend_id": "qxt-main",
  "media_id": "media-123",
  "upload_type": "file",
  "filename": "report.pdf",
  "content_type": "application/pdf"
}
```

`message` 工具将这个对象放入出站消息的附件列表，经 bridge 和 bucket runtime 传回 container-up。
QXT 发送端校验 frontend 后直接使用 `media_id` 发送，不再下载或重复上传。
用户工作目录中的文件路径不会通过上传请求发送给 container-up。

`inbound-sync` 请求产生的附件使用 `reference` 用途，因此 HTTP 调用方收到的仍是下载 URL。
本次调整只改变出站上传和发送；QXT 订阅入站的下载及解析处理保持原有行为。
