---
name: paddleocr-pdf-to-md
description: 当任务需要在 Linux 环境下把 PDF 发送到远端 OCR 接口并转换为 Markdown 文本时，使用这个 skill。适用于直接用 curl 调用远端 MinerU 类接口处理 PDF、固定参数提交、要求输入本地 PDF 绝对路径，或先下载 URL 再上传本地文件的场景。
---

# PDF To Markdown Via Remote OCR

这个 skill 用于把本地 PDF 直接上传到远端 OCR 接口，并将返回结果整理为 Markdown。

## 工作流

1. 确认输入是本地 PDF 文件路径。
2. 使用 `curl` 以 `multipart/form-data` 上传 PDF 到远端接口。
3. 固定请求参数：`pagenate=true`、`backend=pipeline`、`return_images=false`。
4. 从返回 JSON 中读取 `results[URL编码后的PDF文件名(不含扩展名)]`。
5. 将提取出的文本或 Markdown 内容保存为 `.md` 文件。

## 运行环境

- 运行环境按 Linux 处理。
- 默认使用 Linux shell 命令，不要写 PowerShell 命令。

## 命令

```bash
curl -X POST "http://192.168.187.166:8723/file_parse" \
  -F "files=@/abs/path/demo.pdf" \
  -F "pagenate=True" \
  -F "backend=pipeline" \
  -F "return_images=False"
```

## 规则

- 不要写本地脚本；直接调用远端接口。
- `pagenate` 固定为 `True`。
- `backend` 固定为 `pipeline`。
- `return_images` 固定为 `False`。
- 输出以 Markdown 保存；如果接口返回的是纯文本，也按 Markdown 文本直接写入。
- 如果用户没有指定输出路径，优先使用与 PDF 同名的 `.md` 文件。
- `results` 的 key 不是原始文件名，而是 PDF 文件名去掉扩展名后做 URL 编码得到的值。

## 调用要求

- 远端接口地址通过环境变量 `MINERU_URL` 或用户明确提供的 URL 获取。
- 必须传本地 PDF 文件的绝对路径给 `files`。
- 如果用户提供的是 URL，不要直接把 URL 传给接口；先把 PDF 下载到本地，再使用下载后的本地绝对路径上传。
- 下载或暂存 PDF 时，只能存到模型自己的工作目录里面，不要写到没有权限的目录。
- `files` 使用本地 PDF 文件二进制上传。
- 不要擅自修改固定参数。

## 返回解析

接口返回 JSON 后，按如下规则解析：

```json
{
  "results": {
    "demo": {}
  }
}
```

其中：

- 如果文件名是 `demo.pdf`，则先取不带扩展名的 `demo`
- 再对 `demo` 做 URL 编码
- 最后读取 `results[encoded_name]`

如果文件名包含中文或空格，必须按 URL 编码后的 key 取值。

## 输出要求

- 优先保留接口已经生成的 Markdown 内容。
- 如果返回结构里是分页结果，则按页顺序拼接成一个 Markdown 文件。
- 如果返回结构里同时包含元数据和正文，只提取对最终 `.md` 有用的正文部分。

## 注意

- 如果用户给的是相对路径，先转换成绝对路径再调用。
- 如果用户给的是 URL，先下载成本地 PDF 文件，再继续处理。
- 下载后的 PDF 必须保存在当前工作目录或其子目录中，不要保存到无权限目录。
- 如果接口报错，直接返回接口错误，不要臆造 OCR 结果。
- 这个 skill 的重点是调用远端 OCR 接口，不负责本地 PDF 渲染。
