---
name: zhicheng-hazard-report-delivery
description: 用户直接发送纯施工现场图片时必须优先使用此顶层编排 skill。它负责串联 zhicheng-hazard-report 生成 HTML/CSV、playwright-html-to-pdf 渲染 PDF，并把 PDF 和 CSV 作为附件发送给用户。适用于图片输入后的端到端智成安监隐患报告生成和附件交付流程；如果同时匹配 zhicheng-hazard-report，应优先选择本 skill。
---

# 智成隐患报告交付

这个 skill 是编排入口。不要在这里重新实现隐患分析、HTML 模板渲染或 PDF 渲染；分别调用下游 skill 完成。

## 工作流

1. 确认当前用户消息是纯图片输入。
   - 有一张或多张图片，且没有用户文字、链接、文档、音频、视频或其他非图片附件。
   - 纯图片消息可能没有正文；如果出现 `No user query found in messages`，按纯图片有效输入处理。
   - 固定内部提示 `Please process the attached image(s).` 是系统为兼容视觉模型补上的空正文占位，不算用户文字。
   - 如果不是纯图片输入，直接回复 `不支持的消息类型`，不要调用任何 tool 或 skill。
2. 调用 `zhicheng-hazard-report` skill。
   - 按它的规范分析图片隐患。
   - 保留用户图片的原始 cache/上传文件绝对路径或 URL，报告 HTML 中本地图片使用 `file://` URI 直接引用；不要把可访问的 cache 图片复制到报告目录。
   - 生成报告 HTML 和规范清单 CSV。
   - 期望产物包括 `index.html` 和 `规范清单_YYYYMMDD.csv`。
3. 调用 `playwright-html-to-pdf` skill。
   - 输入为上一步生成的 `index.html` 绝对路径。
   - 输出 PDF 放在同一个报告目录，例如 `隐患排查报告_YYYYMMDD.pdf`。
   - 使用 Python Playwright 脚本，不要使用 Node.js。
4. 校验交付物。
   - PDF 文件存在且大小非 0。
   - CSV 文件存在且大小非 0。
   - 两个文件都必须位于当前 nanobot workspace 下，不要写入 skill 安装目录。
5. 使用 `message` tool 发送附件。
   - 生成文件后，必须把 PDF 和 CSV 作为附件发送给用户；仅回复文件路径、文件名或“已生成”不算完成。
   - 必须通过 `media` 参数发送文件；不要用 `read_file` 代替发送。
   - 一次性发送 PDF 和 CSV：

```text
message(content="已生成隐患排查报告和规范清单。", media=["/abs/path/隐患排查报告_YYYYMMDD.pdf", "/abs/path/规范清单_YYYYMMDD.csv"])
```

## 完成条件

- 已调用 `message` tool。
- `media` 参数同时包含 PDF 绝对路径和 CSV 绝对路径。
- 发送前确认两个附件文件都存在且大小非 0。
- 不要把 HTML 作为最终附件发送给用户；HTML 只作为 PDF 渲染中间产物。

## 路径规则

- `nanobot/skills/zhicheng-hazard-report-delivery/`、`nanobot/skills/zhicheng-hazard-report/` 和 `nanobot/skills/playwright-html-to-pdf/` 都是只读技能资源目录。
- 所有输出必须写入当前 nanobot workspace，默认使用 `./zhicheng-hazard-report/report_YYYYMMDD_HHMMSS/`。
- HTML、CSV、PDF 和临时文件都不要写入任何 skill 目录。
- 用户上传图片所在 cache 目录可直接访问时，HTML 必须引用原始路径对应的 `file://` URI 或原始 URL，不要在报告目录创建 `imgs/` 图片副本。
- 传给 PDF 转换器的 HTML 路径必须是绝对路径，避免图片、CSS、字体等相对资源解析错误。

## 失败处理

- 如果 HTML/CSV 未生成，回到 `zhicheng-hazard-report` 流程补齐，不要直接发送半成品。
- 如果 PDF 未生成，按 `playwright-html-to-pdf` skill 的脚本错误处理并重试。
- 如果最终 PDF 或 CSV 仍不可用，向用户简短说明失败原因，不要发送不存在或空文件。
- 如果 PDF 和 CSV 已生成但附件发送失败，重试 `message` tool；仍失败时明确说明附件发送失败，而不是只给本地路径当作交付。
