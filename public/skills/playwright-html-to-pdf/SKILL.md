---
name: playwright-html-to-pdf
description: 使用预装 Chromium 将用户工作区内的 HTML 转为 PDF。
---

执行 `bash /mnt/source/public/skills/playwright-html-to-pdf/scripts/render-html-to-pdf.sh /absolute/input.html /absolute/output/report.pdf`。

输入 HTML、图片、字体等资源必须已在用户目录或授权公共目录中；沙箱不能访问网络。输出必须写用户目录。浏览器与依赖由镜像预装，缺失时报告部署错误，禁止运行时安装。生成文件后调用 `publish_attachment` 发布。
