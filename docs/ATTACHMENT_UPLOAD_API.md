# OpenCode 附件引用、发布与 QXT 渠道发送

CU 入站只传引用，不下载文件、不读取用户工作目录。OpenCode 模型按权限使用文件或网络工具获取内容；图片 URL 也只是文本引用，并非自动注入模型的图片数据。飞书私有资源 URL 带 requires_auth，需要另外提供具备渠道授权的读取工具。

## 发布本地生成文件

模型调用 `publish_attachment(files=[{path, filename?}])`，runtime 校验当前用户、会话和路径，对普通文件创建快照并上传 MinIO，返回 URL 并登记本轮附件。仅能发布当前用户工作目录内的文件；没有配置 attachment_storage 时发布失败。

同步 `/inbound-sync/{frontend_id}` 始终返回可下载 URL。异步 Web 回调带 attachments；QXT/飞书默认发送链接文本。QXT 可在 frontends.json 的对应 frontend 条目中设置 `channel.attachment_delivery: "media"`，由 CU 从发布 URL 下载、上传 QXT，再用 media_id 发往原私聊或群聊。默认值 `links` 保持链接发送。媒体模式需要 CU 能访问 MinIO URL 和 QXT 媒体接口。

## 内部附件引用接口

`POST /internal/attachments/upload` 接收 JSON，不需要 API key；frontend_id 必须已经注册。

| 字段 | 说明 |
| --- | --- |
| frontend_id | 当前配置中的 frontend ID |
| user_id | 可选，仅引用规范化上下文 |
| purpose | reference（默认）或 outbound |
| attachment_url / url | 已有 URL |
| path | 本地路径引用，与 URL 二选一；不会授予读取权限 |
| filename / content_type | 可选文件名与 MIME |

reference 返回规范化引用。outbound + QXT 仅接受 HTTP(S) URL，下载并上传 QXT 后返回 `{storage: "qxt", frontend_id, media_id, upload_type, filename, content_type}`。其他渠道仍返回引用；此接口不发送聊天消息。

QXT 适配器可直接复用同一 frontend 的 media_id，跨 frontend 引用被拒绝。media_id 不是下载 URL，不能作为 OpenCode 入站附件或 runtime 最终结果提交。

业务上传输入文件可使用 hazard-expert-mcp 的 upload_attachment 工具上传 MinIO，再提交返回的 URL。CU 的该接口只接受 JSON，不接收文件字节。
