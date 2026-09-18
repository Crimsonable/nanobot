---
name: compress-image
description: 使用用户工作区内的图片生成压缩副本。
---

先通过 `python3 /opt/public/skills/compress-image/scripts/compress_image.py --help` 查看参数。输入使用 runtime 给出的本地路径，输出放在用户工作目录，不覆盖公共文件。若需返回图片，调用 `publish_attachment`；不要自行调用上传接口。
