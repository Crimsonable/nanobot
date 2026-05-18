---
name: reply-image-minio
description: 处理模型回复文本中的图片引用（Markdown/纯文本）以便用户能在前端看到。只要回复里出现图片引用需求（如 Markdown 图片语法、图片本地路径），必须触发本 skill。若引用已是 http/https 图片 URL，则直接引用原 URL；若引用是本地图片路径，调用本 skill 脚本上传为可访问 URL 后再写入回复。用于所有“在文本中引用图片”的场景，避免输出本地路径或不可访问图片引用。
---

# Markdown Image Minio

当生成直接的回复内容（md/text），如果待引用图片是url链接，则原样引用，如果是本地文件路径则走本 skill，再写入回复内容。

## Trigger (强约束)

出现以下任一情况，立即触发本 skill：

1. 需要在最终回复中放入图片（预览图、截图、结果图、示意图等）。
2. 需要输出任意图片引用形式：
- Markdown：`![alt](...)`
- 纯文本中的图片路径（`.png/.jpg/.jpeg/.webp/.gif/.svg` 等）
3. 用户明确要求“贴图/发图/引用图片链接”。

不满足以上条件时，不调用本 skill。

## Workflow

1. 识别回复里每一个图片引用（`md/html/json/text` 都适用）。
2. 对每个引用做判断：
- 若是 `http://` 或 `https://` 图片 URL，直接使用原引用，不调用脚本。
- 若是本地路径，执行脚本 `scripts/upload_image_to_minio.py`，把路径上传为 URL。
3. 从脚本输出中读取 `url`，再按当前回复格式写入引用。
4. 禁止在最终回复里暴露本地绝对路径或相对路径（如 `/tmp/a.png`、`./a.pdf`、`C:\\x\\y.png`）。

## Command

```bash
python3 nanobot/skills/reply-image-minio/scripts/upload_image_to_minio.py \
  /abs/path/to/file.png
```

引用已是 URL 时，直接在回复中引用该 URL，不调用脚本。

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

- 只要是“图片引用”，必须先执行本 skill 的判定流程。
- 本地路径图片必须先转换为 URL 后再输出。
- 已是 URL 的图片引用直接使用原 URL，不调用脚本。
- 已是 URL 的图片引用禁止下载到本地或写入本地缓存文件。
- 仅在确实要输出文件引用时调用脚本，避免无意义上传。
