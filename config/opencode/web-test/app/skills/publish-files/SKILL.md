---
name: publish-files
description: 向当前用户回复生成的图片、文档和其他附件。
---

调用 `publish_attachment`，参数为 `{"files":[{"path":"report.pdf","filename":"report.pdf"}]}`。支持一次或多次发布多个附件。

不用提供用户 ID、渠道 ID、container_up 地址或存储凭据。工具只接受当前用户工作区内的普通文件；成功表示附件已上传并登记，最终回复由 runtime 汇总、container_up 投递。

不要运行旧的 upload_image_to_minio.py 或直接调用上传接口。不要把本地文件路径当作用户可下载的链接。图片需要在 Markdown 中引用时，使用工具返回的附件 URL。
