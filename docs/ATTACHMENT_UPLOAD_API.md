# 附件链路

入站接口 `/inbound/{frontend_id}` 和 `/inbound-sync/{frontend_id}` 由 Gateway 接收 attachments 中的 URL 或路径引用，经 CU 规范化后转发，不下载文件。

Gateway 在入站 metadata 中写入注册配置确定的 provider 和 frontend_id，覆盖客户端同名字段。CU 原样透传 metadata 到 BU，不实现渠道上传或渠道消息组装。

模型生成文件后调用 `publish_attachment`，参数仍为 files，每个元素包含 path 和可选 filename。BU 验证会话、权限、工作目录和文件大小，生成快照，然后统一调用 Gateway 的 `POST /internal/attachments/{frontend_id}/{request_id}/{attachment_id}`。请求体为文件字节，查询参数为 filename、content_type、sha256，`x-bucket-id` 标识所属桶。模型不能指定渠道、用户或上传凭据。

Gateway 根据该请求所属 frontend 的注册配置分流：

- Web：上传 MinIO，返回 storage=minio、object_key、url。
- QXT：使用该 frontend 的 access_url 获取 access_token，上传至 send_msg_url 同源的 `/v2/media/upload`（multipart 字段 type、media），返回 storage=qxt、frontend_id、media_id、upload_type，不依赖 MinIO。
- 飞书：上传图片或文件，返回 storage=feishu、frontend_id、upload_type 和 image_key/file_key。

Gateway 校验请求与桶归属、活动状态、大小和 SHA-256，并按 frontend_id/request_id/attachment_id 保存上传结果；相同内容重试复用已保存结果，同一 ID 的不同文件被拒绝。上传失败不登记成功引用。外部上传成功但 Gateway 尚未保存结果时进程崩溃，重试仍可能产生多余媒体对象。

BU 将接口返回值原样登记为本轮附件，随最终 `attachments` 通过事件 WS 发回 Gateway。Web 同步接口及回调保留 content 和 attachments；QXT 直接用 media_id 发送 file 消息，飞书直接用媒体 key 发送对应消息。单聊、群聊和回复目标沿用入站的可信 metadata，不重新上传附件，也不把媒体引用拼成文本链接。Gateway 会检查渠道媒体引用确实由当前请求上传。

Web 的 MinIO 配置仍位于 frontend 的 attachment_storage，字段为 endpoint、bucket、key_prefix、presign_expiry_seconds，以及 access_key/secret_key 或各自的凭据文件。配置 public_base_url 时生成公开链接；否则生成签名下载 URL。Gateway 读取凭据并上传，BU 不再加载存储配置或直接访问 MinIO。

工具成功表示文件上传且加入本轮待回复附件。渠道是否收到最终消息，以 Gateway 的 delivery_state 为准。
