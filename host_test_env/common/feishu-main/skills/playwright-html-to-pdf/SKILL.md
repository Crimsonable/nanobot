---
name: playwright-html-to-pdf
description: 当任务需要把本地或远程 HTML 页面渲染成 PDF 时，使用这个 skill。适用于安全报告、日报、仪表盘、静态 HTML 文件、需要保留浏览器 CSS/图片/背景样式、要求用 Playwright/Chromium 真实渲染后导出 PDF 的场景。
---

# Playwright HTML To PDF

这个 skill 用于调用 Playwright Chromium，把 HTML 页面按浏览器渲染结果导出为 PDF。

## 工作流

1. 确认输入 HTML 路径或 URL，以及输出 PDF 路径。
2. 如果输入是相对路径，先转成绝对路径。
3. 输出 PDF 必须放在当前 agent 的 workspace 下；不要写入任何 skill 安装目录。
4. 优先使用本 skill 的脚本：`scripts/render-html-to-pdf.sh`。
5. 如果 Python Playwright 或 Chromium 未安装，按脚本错误提示在当前环境安装依赖后重试。
6. 生成后确认 PDF 文件存在且大小非 0；必要时打开或截图抽查版式。

## 命令

```bash
bash nanobot/skills/playwright-html-to-pdf/scripts/render-html-to-pdf.sh \
  /abs/path/input.html \
  /abs/workspace/output.pdf
```

常用选项：

```bash
bash nanobot/skills/playwright-html-to-pdf/scripts/render-html-to-pdf.sh \
  /abs/path/input.html \
  /abs/workspace/output.pdf \
  --format A4 \
  --media screen \
  --wait-until networkidle \
  --timeout 30000
```

## 脚本参数

- 第一个位置参数：输入 HTML 文件路径，或 `http://`、`https://` URL。
- 第二个位置参数：输出 PDF 文件路径，必须位于当前 agent workspace 下。
- `--format <name>`：纸张尺寸，默认 `A4`。
- `--landscape`：横向输出。
- `--media <screen|print>`：CSS 媒体模式，默认 `screen`，用于尽量保留网页视觉效果。
- `--wait-until <load|domcontentloaded|networkidle>`：页面等待策略，默认 `load`。
- `--timeout <ms>`：页面加载超时时间，默认 `30000`。
- `--no-background`：不打印 CSS 背景；默认打印背景。
- `--no-css-page-size`：不使用 CSS `@page` 尺寸；默认优先使用 CSS 页面尺寸。

## 规则

- 本 skill 的安装目录可能是只读挂载，只能读取 `scripts/render-html-to-pdf.sh`，不能把 PDF、截图或临时文件写入该目录。
- 如果调用方来自 `zhicheng-hazard-report-delivery`，输出 PDF 应放在同一个 workspace 报告目录中，例如 `./zhicheng-hazard-report/report_YYYYMMDD_HHMMSS/隐患排查报告_YYYYMMDD.pdf`。
- 对本地 HTML 使用绝对路径，避免相对资源解析错误。
- 如果 HTML 引用了 `file://` 本地图片，脚本会启用 Chromium 本地文件访问并等待所有图片完成解码后再导出 PDF。
- 如果 HTML 引用了相对路径图片、CSS 或字体，保持 HTML 原文件所在目录结构不变。
- 默认使用 `screen` 媒体模式和 `printBackground: true`，以保留报告类 HTML 的屏幕样式和背景色。
- 如果输出需要严格分页或打印样式，改用 `--media print`。
- 如果页面包含异步渲染、远程图片或图表，优先使用 `--wait-until networkidle`。
- 不要用纯文本 PDF 转换器替代 Playwright；这个 skill 的重点是真实浏览器渲染。

## 依赖

脚本需要 Python 3、Python Playwright 和 Chromium：

```bash
python3 -m pip install playwright
python3 -m playwright install chromium
```

不要依赖 Node.js；agent 环境没有 `node` 时也应使用这个脚本。
