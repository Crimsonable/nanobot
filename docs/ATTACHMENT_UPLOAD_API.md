# 附件链路

入站接口 `/inbound/{frontend_id}` 和 `/inbound-sync/{frontend_id}` 由 Gateway 接收 attachments 中的 URL 或路径引用，经 CU 规范化后转发，不下载文件。

模型生成文件后调用 `publish_attachment`，参数为 files，每个元素包含 path 和可选 filename。runtime 验证文件属于当前用户目录，在 bucket 内上传到该 frontend 配置的 MinIO，返回对象 URL，并把附件加入最终回复。

同步接口与 Web 回调返回 content 和 attachments。QXT、飞书将对象 URL 拼入最终正文作为链接发送；Gateway 不下载、重新上传或转换为渠道媒体。没有单独的后端附件上传接口。

MinIO 配置位于 frontend 的 attachment_storage，字段为 endpoint、bucket、key_prefix、presign_expiry_seconds，以及 access_key/secret_key 或各自的凭据文件。配置 public_base_url 时生成公开链接；否则按明确的签名链接模式生成下载 URL。

工具成功表示文件上传且加入本轮待回复附件。渠道是否收到最终消息，以 Gateway 的 delivery_state 为准。
