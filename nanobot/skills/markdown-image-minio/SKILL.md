---
name: markdown-image-minio
description: 统一处理模型回复中的文件引用（Markdown/JSON/纯文本等格式）。当回复内容里要引用文件时，如果引用是本地路径则先调用本 skill 脚本上传并拿到 URL；如果引用本身已是 http/https URL 则直接使用。用于任何需要把本地文件引用转换为可访问链接的场景。
---

# Markdown Image Minio

当生成回复内容且需要引用文件时，始终先把本地文件路径转换为可访问 URL，再写入回复内容。

## Workflow

1. 识别回复里每一个文件引用（`md/json/text` 都适用）。
2. 对每个引用做判断：
- 若是 `http://` 或 `https://`，直接使用原引用。
- 若是本地路径，执行脚本 `scripts/upload_image_to_minio.py`，把路径上传为 URL。
3. 从脚本输出中读取 `url`，再按当前回复格式写入引用。
4. 禁止在最终回复里暴露本地绝对路径或相对路径（如 `/tmp/a.png`、`./a.pdf`、`C:\\x\\y.png`）。

## Command

```bash
python3 nanobot/skills/markdown-image-minio/scripts/upload_image_to_minio.py \
  /abs/path/to/file.png \
  --containerup-url http://127.0.0.1:8080 \
  --frontend-id web-main \
  --user-id demo-user
```

引用已是 URL 时（脚本会直接透传，不发起上传）：

```bash
python3 nanobot/skills/markdown-image-minio/scripts/upload_image_to_minio.py \
  https://example.com/already-hosted/file.pdf
```

## Script Output Contract

脚本会输出一行 JSON，包含：

- `url`: 可直接引用的链接
- `input_ref`: 输入引用（本地路径或 URL）
- `is_remote`: 输入是否已是远端 URL
- `filename`: 文件名（URL 透传时可能为空）
- `content_type`: 文件类型（可为空）
- `storage`: 存储类型（例如 `minio`，URL 透传时可能为空）
- `bucket`: 对象 bucket（可为空）
- `object_key`: 对象 key（可为空）
- `markdown`: 仅在传入 `--alt` 时提供，值为 `![alt](url)`

对 Markdown 回复可直接使用 `markdown` 字段；对 JSON/文本等格式，使用 `url` 字段。

## Rules

- 本地路径引用必须先转换为 URL 后再输出。
- 已是 URL 的引用禁止再次上传，直接透传使用。
- 仅在确实要输出文件引用时调用脚本，避免无意义上传。
- 同一文件引用可复用已得到的 URL，不必重复上传。
- 如果脚本报错，先说明错误，再继续生成不含该文件引用的回复，或请求可用路径。

## Config

脚本只依赖 container_up 上传接口，至少配置 `container_up` 地址：

- `CONTAINERUP_BASE_URL`（示例：`http://127.0.0.1:8080`）

可选环境变量（未传命令行参数时使用）：

- `CONTAINERUP_FRONTEND_ID`
- `CONTAINERUP_USER_ID`
