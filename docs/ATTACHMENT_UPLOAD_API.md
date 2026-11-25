# 附件发布链路

Gateway 在入站 metadata 写入注册的 provider/frontend_id，渠道与用户归属以已保存请求为准。

模型调用 `publish_attachment({files: [{path, filename?}]})`。插件通过原生会话/消息 metadata 确定用户、请求与截止时间，直接读取权限文件并验证目录范围。插件按需启动本地 `snapshot.py`，在会话锁和配额锁下复制普通单链接文件，校验每层路径、大小和 SHA-256；不启动 Python HTTP 服务。

插件随后直接调用：

```http
POST /internal/attachments/{frontend_id}/{request_id}/{attachment_id}?filename=report.pdf&content_type=application/pdf&sha256=...
x-bucket-id: <bucket>
x-root-session-id: <native root session>
Content-Type: application/octet-stream

<文件字节>
```

Gateway 校验请求、桶、根会话、截止时间与取消状态，上传结束后再次校验，才登记为本轮附件。重复 attachment_id 必须对应相同内容，成功上传不会再次上传。插件核对返回 ID、文件名、大小和摘要，返回 `queued_for_reply` 与附件引用；模型不能指定渠道、用户或凭据。

| 渠道 | Gateway 上传方式 | 返回引用 |
| --- | --- | --- |
| Web | frontend 的 MinIO attachment_storage | url、object_key、filename、size、sha256 |
| QXT | 渠道媒体上传 API | media_id、storage、frontend_id |
| 飞书 | 文件/图片上传 API | file_key 或 image_key、upload_type、storage、frontend_id |

最终回复由 Gateway 直接把已登记附件加入 attachments。QXT、飞书沿用可信入站目标发送媒体引用，不重复上传、不拼造下载链接。只有 Web 的真实 HTTP URL 才作为下载链接使用。

Web MinIO 配置仍放在 frontend.attachment_storage：endpoint、bucket、key_prefix、presign_expiry_seconds，以及 access_key/secret_key 或各自文件。public_base_url 生成公开地址，否则返回签名地址。只有 Gateway 读取这些存储凭据。

入站附件是引用；Gateway/CU 不预下载。模型通过已授权的工具读取 URL。插件只发布当前用户 workspace 的文件，不允许发布公共目录或服务配置。
