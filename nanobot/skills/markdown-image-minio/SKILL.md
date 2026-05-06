---
name: markdown-image-minio
description: 约束 Markdown 生成中的图片引用流程：当输出内容包含本地图片时，先调用本 skill 的 MinIO 上传脚本，把本地图片路径上传为远端 URL，再按标准 Markdown 图片语法插入引用。用于生成报告、说明文档、周报、方案文档等任何可能包含 `![alt](...)` 图片链接的场景。
---

# Markdown Image Minio

当生成 Markdown 且需要插入图片时，始终先把本地图片上传到 MinIO，再输出 Markdown 图片引用。

## Workflow

1. 识别是否需要图片引用。
2. 对每一张本地图片，先执行脚本：`scripts/upload_image_to_minio.py`。
3. 从脚本输出中获取 `url` 或 `markdown` 字段。
4. 在最终 Markdown 中使用标准语法：`![alt text](image_url)`。
5. 不要在 Markdown 中保留本地路径（如 `/tmp/a.png`、`C:\\x\\y.png`）。

## Command

```bash
python3 nanobot/skills/markdown-image-minio/scripts/upload_image_to_minio.py \
  /abs/path/to/image.png
```

带 alt 文本：

```bash
python3 nanobot/skills/markdown-image-minio/scripts/upload_image_to_minio.py \
  /abs/path/to/image.png \
  --alt "架构图"
```

## Script Output Contract

脚本会输出一行 JSON，包含：

- `url`: 图片公网访问链接
- `markdown`: 标准 Markdown 图片引用
- `bucket`: MinIO bucket 名称
- `object_key`: MinIO 对象键
- `content_type`: 文件类型

优先直接使用 `markdown` 字段；如需自定义 alt 文本，可用 `url` 重新拼接 `![自定义alt](url)`。

## Rules

- 必须先上传再引用，禁止直接输出本地文件路径图片引用。
- 仅在确实需要图片时调用脚本，避免无意义上传。
- 同一图片可复用已得到的 URL，不必重复上传。
- 如果脚本报错，先说明错误，再继续生成不含该图片的 Markdown 或请求可用图片路径。

## MinIO Defaults

脚本内置以下配置（可通过环境变量覆盖）：

- `endpoint`: `http://192.168.48.104:9000`
- `access_key`: `minio_admin`
- `secret_key`: `minio_password`
- `bucket`: `attachments`
- `public_base_url`: `http://192.168.48.104:9000/attachments`
