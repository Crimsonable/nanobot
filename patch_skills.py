#!/usr/bin/env python3
"""Rewrite legacy upload-based skills to use the publish_attachment tool."""
import io, json, os, shutil, sys
from pathlib import Path

def read(p): return Path(p).read_text(encoding="utf-8")
def write(p, t):
    Path(p).write_text(t, encoding="utf-8")
    print("wrote", p)

def sub_once(text, old, new, label):
    if old not in text:
        raise SystemExit(f"MISS: {label}")
    return text.replace(old, new, 1)

REPLY_SKILL = """---
name: reply-image-minio
description: 处理模型回复文本中的图片引用（Markdown/纯文本）。回复里出现图片引用需求时触发：已是 http/https 图片 URL 的直接引用；本地图片必须先通过 `publish_attachment` 发布，再使用工具返回的结果写入回复。禁止调用任何上传接口或旧上传脚本。
---

# 回复中的图片引用

当生成回复内容（md/text/html/json）时需要引用图片，按以下规则处理。

## Workflow

1. 识别回复中的每一个图片引用。
2. 已是 `http://` 或 `https://` 的图片 URL：原样引用，不下载、不重复上传。
3. 本地图片文件：调用 `publish_attachment`

```json
{"files":[{"path":"图片路径","filename":"文件名.png"}]}
```

4. 按工具返回值写回复：
   - 返回 `url`（Web 渠道）：用该 URL 写 Markdown `![alt](url)` 或纯文本链接。
   - 返回 `media_id` 等媒体引用（QXT / 飞书渠道）：不要伪造 URL，也不要写 Markdown 图片；附件由 Gateway 随本轮回复发送。
5. 最终回复里不得出现本地绝对路径或相对路径。

## Rules

- 只接受当前用户工作目录内的普通文件；工作目录外的图片先复制进工作目录再发布。
- 不要运行 `upload_image_to_minio.py` 等旧脚本，不要直接调用 `/internal/attachments/upload` 或任何对象存储接口。
- 同一张图片只发布一次，复用工具返回结果。
- 工具报错时如实说明错误，不要退回到本地路径引用。
"""

MARKDOWN_SKILL = """---
name: markdown-image-minio
description: 生成 Markdown 时插入本地图片：先调用 `publish_attachment` 发布图片，再把返回的 URL 写入 `![alt](url)`。禁止使用旧上传脚本或直接调用对象存储接口。
---

# Markdown 中的图片引用

当生成 Markdown（报告、说明文档、周报、方案等）需要插入本地图片时：

## Workflow

1. 识别需要插入的本地图片。
2. 调用 `publish_attachment`

```json
{"files":[{"path":"图片路径","filename":"文件名.png"}]}
```

3. 按工具返回值处理：
   - 返回 `url`（Web 渠道）：在 Markdown 中写 `![alt](url)`。
   - 返回 `media_id` 等媒体引用（QXT / 飞书渠道）：不要伪造 URL，也不要写图片语法；图片由 Gateway 作为附件单独发送，正文里用文字说明即可。
4. Markdown 中不得保留本地路径（如 `/tmp/a.png`、`C:\\x\\y.png`）。

## Rules

- 只接受当前用户工作目录内的普通文件；工作目录外的图片先复制进工作目录。
- 不要运行 `upload_image_to_minio.py` 等旧脚本，不要直接调用 MinIO 或其他对象存储接口。
- 同一张图片只发布一次，复用工具返回结果。
- 工具报错时如实说明，不要退回到本地路径引用。
"""

def patch_image_skill(d, kind):
    d = Path(d)
    write(d / "SKILL.md", REPLY_SKILL if kind == "reply" else MARKDOWN_SKILL)
    scripts = d / "scripts"
    if scripts.is_dir():
        shutil.rmtree(scripts)
    nested = d / d.name
    if nested.is_dir():
        shutil.rmtree(nested)
        print("removed nested duplicate", nested)
    for pyc in d.rglob("__pycache__"):
        shutil.rmtree(pyc, ignore_errors=True)

REPORT_TAIL_NEW = '''    output_dir = input_json_path.resolve().parent
    outputs = []
    for filename, content, content_type in report_outputs:
        target = output_dir / filename
        target.write_bytes(content)
        outputs.append({"path": str(target), "filename": filename, "content_type": content_type})
    print(json.dumps({"files": outputs, "delivery": "call publish_attachment with these paths"},
                     ensure_ascii=False))
'''

def cut_tail(text):
    start = text.index("    uploaded_outputs: dict[str, str] = {}")
    end_marker = '    print(json.dumps(uploaded_outputs, ensure_ascii=False))\n'
    end = text.index(end_marker, start) + len(end_marker)
    return text[:start] + REPORT_TAIL_NEW + text[end:]

def patch_hazard(d, flavor):
    d = Path(d)
    rg = d / "scripts" / "report_generator.py"
    t = read(rg)
    for line in ['DEFAULT_UPLOAD_TARGET = ', 'DEFAULT_FRONTEND_ID = ', 'DEFAULT_USER_ID = ']:
        lines = [l for l in t.splitlines(True) if not l.startswith(line)]
        t = "".join(lines)
    start = t.index("def _guess_content_type(")
    end = t.index("class ReportGenerator:")
    t = t[:start] + t[end:]
    t = sub_once(t, "import httpx\n", "", "httpx import")
    if flavor == "formal":
        t = sub_once(t, 'SCRIPT_CONFIG_PATH = SCRIPT_DIR / "report_generator.config.json"\n', "", "SCRIPT_CONFIG_PATH")
        t = sub_once(t, "    config = _load_script_config()\n", "", "_load_script_config call")
        t = sub_once(t, """    try:
        input_json_bytes = input_json_path.read_bytes()
    except Exception as exc:
        print(f"读取输入 JSON 失败: {exc}")
        return 1

""", "", "input_json_bytes block")
    else:
        t = sub_once(t, """    try:
        input_json_bytes = input_json_path.read_bytes()
    except Exception as exc:
        print(f"读取输入 JSON 失败: {exc}")
        return 1

""", "", "input_json_bytes block")
    t = cut_tail(t)
    write(rg, t)

    if flavor == "formal":
        sk = d / "SKILL.md"
        s = read(sk)
        s = sub_once(s,
            "`report_generator.py` 读取这些 JSON 后生成并上传最终产物",
            "`report_generator.py` 读取这些 JSON 后生成最终产物并打印 `files` 清单",
            "SKILL output bullet")
        s = sub_once(s,
            "- 默认先生成 PDF，再询问是否需要 Word",
            "- 生成后必须调用 `publish_attachment`，参数为该清单中的 path / filename；不要自行上传\n- 默认先生成 PDF，再询问是否需要 Word",
            "SKILL delivery line")
        write(sk, s)
    else:
        sk = d / "SKILL.md"
        s = read(sk)
        s = sub_once(s, "收尾（组装→校验→渲染→自动上传）",
                     "收尾（组装→校验→渲染→输出文件清单），最后调用 `publish_attachment` 交付产物",
                     "SKILL step3")
        s = sub_once(s, "- 上传不是独立步骤：调用 `scripts/finalize_report.py` 即自动上传产物并返回各产物 URL",
                     "- 交付不是脚本的职责：`scripts/finalize_report.py` 只生成文件并打印 `files` 清单，随后调用 `publish_attachment`",
                     "SKILL upload line")
        s = sub_once(s, "收尾（组装→校验→渲染→自动上传）｜ `scripts/report_prep_lib.py`",
                     "收尾（组装→校验→渲染→输出文件清单）｜ `scripts/report_prep_lib.py`",
                     "SKILL script line")
        write(sk, s)

        wf = d / "references" / "workflow.md"
        w = read(wf)
        w = sub_once(w, "→ 渲染 → 自动上传」", "→ 渲染 → 输出文件清单」", "workflow finalize line")
        w = sub_once(w, "- 上传不是独立步骤：渲染成功即视为已自动上传，脚本会打印各产物 URL",
                     "- 交付不是脚本的职责：脚本只打印产物路径，必须再调用 `publish_attachment` 把这些文件加入本轮回复",
                     "workflow upload line")
        write(wf, w)

        fr = d / "scripts" / "finalize_report.py"
        f = read(fr)
        f = sub_once(f, "渲染并自动上传", "渲染并输出文件清单", "finalize docstring title")
        f = sub_once(f, "  - 渲染并自动上传复用 scripts/report_generator.py（ReportGenerator / ObjectStorageUploader），",
                     "  - 渲染复用 scripts/report_generator.py（ReportGenerator），", "finalize docstring body")
        f = sub_once(f, "  - 渲染成功即视为已自动上传，上传不是独立步骤；\n", "  - 脚本只写文件并打印 files 清单，交付由模型调用 publish_attachment 完成；\n", "finalize docstring upload")
        f = sub_once(f, "或渲染/上传失败", "或渲染失败", "finalize exitcodes")
        f = sub_once(f, """    from report_generator import (  # noqa: E402
        DEFAULT_FRONTEND_ID,
        DEFAULT_UPLOAD_TARGET,
        DEFAULT_USER_ID,
        ObjectStorageUploader,
        ReportGenerator,
        _upload_report_bundle,
    )""", "    from report_generator import ReportGenerator  # noqa: E402", "finalize import")
        f = sub_once(f, "def _render_and_upload(", "def _render_outputs(", "finalize func name")
        f = f.replace("_render_and_upload(", "_render_outputs(")
        f = sub_once(f, '''    uploader = ObjectStorageUploader(
        DEFAULT_UPLOAD_TARGET,
        frontend_id=DEFAULT_FRONTEND_ID,
        user_id=DEFAULT_USER_ID,
    )
    try:
        uploaded_outputs = _upload_report_bundle(
            uploader=uploader,
            report_outputs=report_outputs,
            input_json_bytes=input_json_bytes,
            base_name=base_name,
        )
    except Exception as exc:
        print(f"上传失败: {exc}")
        return 1

    print(json.dumps(uploaded_outputs, ensure_ascii=False))
    return 0''', '''    output_dir = input_json_path.resolve().parent
    outputs = []
    for filename, content, content_type in report_outputs:
        target = output_dir / filename
        target.write_bytes(content)
        outputs.append({"path": str(target), "filename": filename, "content_type": content_type})
    print(json.dumps({"files": outputs, "delivery": "call publish_attachment with these paths"},
                     ensure_ascii=False))
    return 0''', "finalize upload block")
        f = sub_once(f, '''    try:
        data = json.loads(input_json_path.read_text(encoding="utf-8"))
        input_json_bytes = input_json_path.read_bytes()
    except Exception as exc:''', '''    try:
        data = json.loads(input_json_path.read_text(encoding="utf-8"))
    except Exception as exc:''', "finalize read bytes")
        write(fr, f)

    for pyc in d.rglob("__pycache__"):
        shutil.rmtree(pyc, ignore_errors=True)

if __name__ == "__main__":
    mode, target = sys.argv[1], sys.argv[2]
    if mode == "reply":
        patch_image_skill(target, "reply")
    elif mode == "markdown":
        patch_image_skill(target, "markdown")
    elif mode == "hazard-formal":
        patch_hazard(target, "formal")
    elif mode == "hazard-sv":
        patch_hazard(target, "sv")
    else:
        raise SystemExit("unknown mode " + mode)
    print("OK", mode, target)