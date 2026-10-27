# 隐患识别与分析 MCP 服务

独立运行的 Streamable HTTP MCP 服务，地址为 `/mcp`，只注册两个工具：

| 工具 | 用途 |
| --- | --- |
| `upload_attachment`（上传附件至 MinIO） | 接收文件内容的 Base64，上传 MinIO，返回预签名下载 URL |
| `hazard_analysis_expert`（隐患识别与分析专家） | 将当前用户 ID、会话 ID、问题及附件 URL 转发给 channel-gateway 的 `inbound-sync`，等待最终回答 |

专家能力由当前 frontend 的 OpenCode 模型、提示词及技能提供：模型/provider 配置位于 `common/<frontend_id>/config/opencode-server.json`，用户权限位于同目录的 `user-permissions.yaml`，业务资料和技能位于 `app/`。本服务是业务客户端的入口，内部分析工具调用 Gateway；不要将它注册为同一后端的 OpenCode 模型工具，以免递归调用自身。
上传直接使用 MinIO S3 API，无需依赖 channel-gateway 上传接口，也不自动创建桶。

## Docker Compose 部署

修改本目录 **`docker-compose.yml`** 中的配置。本服务的环境变量集中在此文件，源码通过只读挂载加载，镜像只提供 Python 与依赖：

1. 设置 `CONTAINER_UP_BASE_URL` 为已部署的 channel-gateway 地址；示例通过宿主机的 NodePort 30081 访问 Kubernetes Gateway。本地直接启动 Gateway 时改用其实际监听端口，默认 8000。
2. 设置 `CONTAINER_UP_FRONTEND_ID` 为 `common/frontends.json` 中已注册的 frontend，示例为 `web-test`。会话与工作空间按 frontend 和 user_id 隔离。
3. 设置 MinIO API 地址、桶名、凭证。桶需存在，凭证需有该桶目标前缀的 `PutObject` 和 `GetObject` 权限。
4. 设置 `MINIO_DOWNLOAD_ENDPOINT` 为调用方及 OpenCode 工具都可访问的同一 MinIO S3 服务地址。它是 `https://files.example.com` 这样的 origin，不包含桶名、路径前缀、查询参数。生成链接时直接对此地址签名，不能在生成后替换域名。留空则沿用 `MINIO_ENDPOINT`。
5. 替换 `MCP_API_KEY`。服务不限制 MCP 请求的 Host 或 Origin，无需配置域名或来源白名单。浏览器跨站调用需要的 CORS 响应头可在网关配置。

```bash
cd hazard_expert_mcp
docker compose up -d --build
docker compose logs -f hazard-expert-mcp
```

默认 MCP 地址：`http://localhost:8082/mcp`。健康检查：`http://localhost:8082/healthz`。
Compose 只启动此 MCP 服务，复用现有 channel-gateway 和 MinIO。
`host.docker.internal` 示例用于访问 Docker 宿主机发布的端口；如果后端在其他机器，直接填写实际地址。
Compose 将项目源码只读挂载到 `/mnt/source`，镜像以 UID 10001 运行。更新源码后重启服务即可，依赖变化时重建镜像。

渠道分层不改变本服务的调用代码、请求/响应协议及 `CONTAINER_UP_*` 配置名。本服务直接调用 Gateway 的普通 Web 同步接口；原外部入口地址保持不变时无需修改配置，否则只调整 `CONTAINER_UP_BASE_URL`。

## Kubernetes 部署

使用仓库根目录的 [`k8s/opencode/hazard-expert-mcp.yaml`](../k8s/opencode/hazard-expert-mcp.yaml)。
与 engineering MCP 一样，采用 `opencode-sv` Namespace、`opencode-runtime=true` 节点选择、`opencode-source-pvc` 只读源码挂载和 NodePort Service。
集群部署的全部运行配置位于该 YAML 的 Deployment `env` 中，无需额外 ConfigMap、Secret 或 JSON 配置文件。
Compose 与 Kubernetes 是两种部署入口，分别编辑各自的环境变量。

1. 按 [OpenCode 部署说明](../k8s/opencode/README.md) 准备 `storage.yaml` 中的 Namespace/PVC 和 `backend.yaml` 中的 Gateway/CU Pod，目标节点具有 `opencode-runtime=true` 标签。
2. 将本目录同步到源码 PVC 根目录下的 `hazard_expert_mcp/`，使容器中的 `/mnt/source/hazard_expert_mcp/hazard_expert_mcp/app.py` 存在。与 engineering MCP 相同，Kubernetes 从挂载的源码目录启动。源目录和文件需允许镜像的 UID 10001 读取。
3. 修改清单中的镜像地址、`CONTAINER_UP_FRONTEND_ID`、MCP 服务访问凭证和 MinIO 凭证；存储桶必须已创建。默认后端为 `http://channel-gateway.opencode-sv.svc.cluster.local:8000`，调用 Gateway 不需要 API key。
4. 外部客户端需要下载附件时，设置 `MINIO_DOWNLOAD_ENDPOINT` 为双方可访问的 S3 地址。

从仓库根目录构建并部署：

```bash
docker build -f hazard_expert_mcp/Dockerfile -t hazard-expert-mcp:1.0.0 hazard_expert_mcp
# 将镜像推送到集群可访问的仓库并更新 YAML 的 image，或导入目标节点的容器运行时。
kubectl apply -f k8s/opencode/hazard-expert-mcp.yaml
kubectl rollout status deployment/hazard-expert-mcp -n opencode-sv
kubectl logs -n opencode-sv deployment/hazard-expert-mcp -f
```

- 集群内 MCP 地址：`http://hazard-expert-mcp.opencode-sv.svc.cluster.local:8082/mcp`
- 集群外 MCP 地址：`http://<节点 IP>:30088/mcp`
- 探针地址：`/healthz`

只更新挂载源码后，可执行 `kubectl rollout restart deployment/hazard-expert-mcp -n opencode-sv`；依赖发生变化时需要重建镜像。
部署到其他命名空间时，同步修改 Namespace、PVC、节点选择、channel-gateway 服务地址以及 NodePort。

## 客户端连接

可信业务调用方需携带：

```http
Authorization: Bearer <部署配置中的 MCP_API_KEY>
```

这是服务级鉴权，不是用户登录或 OAuth。**调用方必须从当前登录用户获得 `user_id`，并绑定到工具调用，不能允许不可信客户端随意选择其他用户的 ID。**
当前需求显式传入 `user_id`，本服务会原样转发，不额外引入账号映射。调用 Gateway 不发送 Bearer 凭据；MCP_API_KEY 仍仅用于客户端访问本 MCP 服务。

支持远程 HTTP MCP 的客户端配置通常类似下面这样，具体字段以客户端为准；不要选旧版 SSE 传输：

```json
{
  "mcpServers": {
    "hazard-expert": {
      "type": "http",
      "url": "http://localhost:8082/mcp",
      "headers": {
        "Authorization": "Bearer <配置的服务访问凭证>"
      }
    }
  }
}
```

使用本项目依赖中的 MCP Python SDK 时，可在业务代码里先读取文件，再完成上传与分析：

```python
import base64
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def inspect_site(mcp_url, service_token, current_user_id, chat_id, local_file):
    path = Path(local_file)
    async with streamablehttp_client(
        mcp_url,
        headers={"Authorization": f"Bearer {service_token}"},
        timeout=750,
        sse_read_timeout=750,
    ) as (read, write, _):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=750)) as session:
            await session.initialize()
            uploaded = await session.call_tool("upload_attachment", {
                "user_id": current_user_id,
                "filename": path.name,
                "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            })
            if uploaded.isError:
                raise RuntimeError(uploaded.content)
            result = await session.call_tool("hazard_analysis_expert", {
                "user_id": current_user_id,
                "chat_id": chat_id,
                "query": "请识别现场隐患并提出整改措施。",
                "attachments": [uploaded.structuredContent["url"]],
            })
            if result.isError:
                raise RuntimeError(result.content)
            return result.structuredContent
```

## 工具参数

### 上传附件

```json
{
  "user_id": "user-123",
  "filename": "现场照片.jpg",
  "content_base64": "<文件字节的标准 Base64>",
  "content_type": "image/jpeg"
}
```

`content_type` 可省略，按扩展名推断。`content_base64` 不含 `data:...;base64,` 前缀或换行，必须由客户端读取文件并编码，不能让模型编造文件内容。
HTTP MCP 服务无法读取调用方电脑上的路径，因此不接收本地文件路径。默认单文件上限 25 MiB，Base64 会增加约三分之一传输体积；还需遵守 MCP 客户端自身的工具请求大小限制。

返回的结构化结果示例：

```json
{
  "url": "https://files.example.com/attachments/hazard-expert/...?X-Amz-Signature=...",
  "filename": "现场照片.jpg",
  "content_type": "image/jpeg",
  "size_bytes": 123456,
  "bucket": "attachments",
  "object_key": "hazard-expert/<用户ID哈希>/2026/09/21/<随机ID>-现场照片.jpg",
  "expires_at": "2026-09-22T08:00:00+00:00"
}
```

下载链接默认有效 24 小时（最多 7 天），无需公开桶。到期后持久化对象仍存在；本服务没有额外注册续签或删除工具。上传不会覆盖同名对象。
历史会话重读附件仍受链接有效期约束。CU/runtime 不自动下载或缓存入站附件；需要长期使用时，由 OpenCode 工具按权限保存到该用户工作空间，或重新上传并提供新的链接。

### 隐患识别与分析

```json
{
  "user_id": "user-123",
  "chat_id": "inspection-20260921-001",
  "query": "请分析现场照片中的安全隐患，给出风险和整改建议。",
  "attachments": ["<upload_attachment 返回的 url>"]
}
```

`attachments` 是 HTTP(S) URL 字符串数组，最多 20 个，可省略或传 `[]`。也可直接使用已有的可下载 URL。
URL 会保留完整查询字符串，支持预签名链接。读取工具所在环境须能访问这些 URL；Bash 下载需为该用户开启 `sandbox.network`。图片 URL 在 CU 中只作为引用传递，实际视觉分析需在对应 frontend 配置可读取图片的工具或图像分析 MCP。

上游请求精确映射为：

```http
POST <CONTAINER_UP_BASE_URL>/inbound-sync/<CONTAINER_UP_FRONTEND_ID>
Content-Type: application/json
```

```json
{
  "user_id": "user-123",
  "chat_id": "inspection-20260921-001",
  "content": "请分析现场照片中的安全隐患，给出风险和整改建议。",
  "attachments": ["<附件 URL>"]
}
```

工具返回 `status`、`user_id`、`chat_id`、`content`、`attachments`。上游桶 ID、实例 ID 和内部 metadata 不作为专家工具输出。
后续调用使用同一 `user_id` 和 `chat_id`，例如 `query="第二项隐患应如何整改？"`，即可继续原会话；更换 `chat_id` 则使用另一个会话。

会话由 OpenCode 后端按 `frontend_id + user_id + chat_id` 管理。MCP 连接 ID 不参与会话标识，重新连接或重启也不会生成新的 chat_id。运行中的 bucket 复用对应 OpenCode session，bucket 回收后由 CU 调度迁移持久化的 OpenCode 原生会话。同一用户请求由 runtime 串行执行。

## 超时和运行约束

- `CONTAINER_UP_TIMEOUT_SECONDS` 默认 720 秒，覆盖上游默认 600 秒同步等待及实例冷启动。MCP 客户端、反向代理的请求/读取超时也需要相应调大。
- 上游请求不自动重试。超时、断网或 MCP 客户端断开并不保证后端任务被取消；避免自动重复提交造成重复执行。
- Gateway 保存请求、最终结果及渠道投递状态，BU 通过独立事件 WebSocket 回传；CU 只处理调度及控制状态。Gateway 与 CU 共用一个单副本 Pod、各自单 worker 和独立 SQLite，不能让多个写入进程共用同一数据库。
- `/healthz` 只检查 MCP 进程存活，不保证 channel-gateway 或 MinIO 可用。

## 本地验证

```bash
python -m venv .venv
# Linux/macOS: .venv/bin/python；Windows: .venv/Scripts/python.exe
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests
```

测试通过 MCP HTTP JSON-RPC 验证工具发现、两个工具调用、鉴权、请求限制，并验证上游参数转发、超时处理和 MinIO 签名地址。
测试使用模拟的 channel-gateway 响应与 S3 Stubber，不依赖真实业务账号或对象存储凭证。

实现参考：[MCP Python SDK 1.26.0](https://github.com/modelcontextprotocol/python-sdk/tree/v1.26.0)、[Boto3 预签名下载链接](https://docs.aws.amazon.com/boto3/latest/guide/s3-presigned-urls.html)。
