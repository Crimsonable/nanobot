---
name: publish-files
description: 向当前用户回复生成的图片、文档和其他附件。
---

调用 `publish_attachment`，参数为 `{"files":[{"path":"report.pdf","filename":"report.pdf"}]}`。支持一次或多次发布多个附件。

不用提供用户 ID、渠道 ID、上传地址或存储凭据。工具只接受当前用户工作区内的普通文件；runtime 校验后统一调用 Gateway 上传，成功表示附件已上传并登记，最终回复由 Gateway 按渠道投递。

不要运行旧的 upload_image_to_minio.py 或直接调用上传接口。不要把本地文件路径当作用户可下载的链接。Web 返回 URL，可用于 Markdown 图片；QXT/飞书返回媒体引用，由 Gateway 发送附件，不能把 media_id 或媒体 key 当作 URL。
