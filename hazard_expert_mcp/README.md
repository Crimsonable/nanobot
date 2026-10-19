# 隐患识别与分析 MCP 服务

独立运行的 Streamable HTTP MCP 服务，地址为 `/mcp`，注册三个工具：

| 工具 | 用途 |
| --- | --- |
| `upload_attachment`（上传附件至 MinIO） | 接收文件内容的 Base64，上传 MinIO，返回预签名下载 URL |
| `hazard_analysis_expert`（隐患识别与分析专家） | 将当前用户 ID、会话 ID、问题及附件 URL 转发给 container-up 的 `inbound-sync`，等待最终回答 |
| `query_analysis_result`（查询隐患分析结果） | 按 user_id 和 chat_id 查询本地保存的分析状态、问题和最终结果 |

只增加调用入口。专家能力由所选 frontend 对应的 nanobot 配置、提示词及技能提供。
上传直接使用 MinIO S3 API，无需依赖 container-up 上传接口，也不自动创建桶。

## Docker Compose 部署

当前 Compose 和 Kubernetes 清单均已配置后端 `http://192.168.199.168:30081`、frontend `web-test`，分析请求发送至 `http://192.168.199.168:30081/inbound-sync/web-test`。镜像只包含 Python 和依赖，业务配置由部署文件在运行时注入。Compose 和 Kubernetes 已配置 MinIO 上传及下载签名地址 `http://192.168.199.167:9000`、桶 `attachments` 和部署凭证，对应文件地址前缀 `http://192.168.199.167:9000/attachments`。MinIO 配置在运行时注入，不写入镜像。MCP 服务凭证仍需按实际部署填写。

修改本目录 **`docker-compose.yml`** 中的配置，所有运行配置均放在此文件，不需要 `.env` 或 JSON 配置文件；源码必须通过目录挂载：

1. 设置 `CONTAINER_UP_BASE_URL` 为已部署的 container-up 地址。
2. 设置 `CONTAINER_UP_FRONTEND_ID` 为其 `frontends.json` 中已有的 frontend；沿用原 frontend 才能进入该用户原来的工作空间和实例。
3. 设置 MinIO API 地址、桶名、凭证。桶需存在，凭证需有该桶目标前缀的 `PutObject` 和 `GetObject` 权限。
4. 设置 `MINIO_DOWNLOAD_ENDPOINT` 为调用方及 nanobot 都可访问的同一 MinIO S3 服务地址。它是 `https://files.example.com` 这样的 origin，不包含桶名、路径前缀、查询参数。生成链接时直接对此地址签名，不能在生成后替换域名。留空则沿用 `MINIO_ENDPOINT`。
5. 替换 `MCP_API_KEY`。服务不限制 MCP 请求的 Host 或 Origin，无需配置域名或来源白名单。浏览器跨站调用需要的 CORS 响应头可在网关配置。

```bash
cd hazard_expert_mcp
docker compose up -d --build
docker compose logs -f hazard-expert-mcp
```

默认 MCP 地址：`http://localhost:48082/mcp`。健康检查：`http://localhost:48082/healthz`。
Compose 只启动此 MCP 服务，复用现有 container-up 和 MinIO。
`host.docker.internal` 示例用于访问 Docker 宿主机发布的端口；如果后端在其他机器，直接填写实际地址。
镜像只提供运行环境，不包含业务源码。Compose 将同目录下的 `hazard_expert_mcp/` 只读挂载到 `/app/hazard_expert_mcp`；该目录必须存在并包含 `app.py`、`history.py` 等完整模块，且允许 UID 10001 读取。SQLite 数据单独保存在 `hazard-history` 卷。修改代码后执行 `docker compose restart hazard-expert-mcp`；只有依赖变更才需重建镜像。

## Kubernetes 部署

使用仓库根目录的 [`k8s/base/hazard-expert-mcp.yaml`](../k8s/base/hazard-expert-mcp.yaml)。
与 engineering MCP 一样，采用 `nanobot` Namespace、`nanobot_node=prod` 节点选择、`nanobot-source-pvc` 只读源码挂载和 NodePort Service。
集群部署的全部运行配置位于该 YAML 的 Deployment `env` 中，无需额外 ConfigMap、Secret 或 JSON 配置文件。
Compose 与 Kubernetes 是两种部署入口，分别编辑各自的环境变量。

1. 确保现有 `k8s/base/base.yaml` 提供的 Namespace 和源码 PVC 已就绪，目标节点具有 `nanobot_node=prod` 标签。
2. 将本目录同步到源码 PVC 根目录下的 `hazard_expert_mcp/`，使容器中的 `/mnt/source/hazard_expert_mcp/hazard_expert_mcp/app.py` 存在。与 engineering MCP 相同，Kubernetes 从挂载的源码目录启动。源目录和文件需允许镜像的 UID 10001 读取。
3. 确认清单中的镜像地址并设置 MCP 服务访问凭证；MinIO 桶必须已创建。后端已配置为 `http://192.168.199.168:30081`、frontend `web-test`；MinIO 已配置为 `http://192.168.199.167:9000`、桶 `attachments`。
4. 外部客户端需要下载附件时，设置 `MINIO_DOWNLOAD_ENDPOINT` 为双方可访问的 S3 地址。

从仓库根目录构建并部署：

```bash
docker build -f hazard_expert_mcp/Dockerfile -t hazard-expert-mcp:1.0.0 hazard_expert_mcp
# 将镜像推送到集群可访问的仓库并更新 YAML 的 image，或导入目标节点的容器运行时。
kubectl apply -f k8s/base/hazard-expert-mcp.yaml
kubectl rollout status deployment/hazard-expert-mcp -n nanobot
kubectl logs -n nanobot deployment/hazard-expert-mcp -f
```

- 集群内 MCP 地址：`http://hazard-expert-mcp.nanobot.svc.cluster.local:8082/mcp`
- 集群外 MCP 地址：`http://<节点 IP>:30088/mcp`
- 探针地址：`/healthz`

只更新挂载源码后，可执行 `kubectl rollout restart deployment/hazard-expert-mcp -n nanobot`；依赖发生变化时需要重建镜像。
如果部署到现有 `nanobot-test` 环境，需要同步修改清单的 Namespace、节点标签为 `test`、container-up 服务地址中的命名空间，以及另一个未占用的 NodePort。

## 客户端连接

可信业务调用方需携带：

```http
Authorization: Bearer <部署配置中的 MCP_API_KEY>
```

这是服务级鉴权，不是用户登录或 OAuth。**调用方必须从当前登录用户获得 `user_id`，并绑定到工具调用，不能允许不可信客户端随意选择其他用户的 ID。**
当前需求显式传入 `user_id`，本服务会原样转发，不额外引入账号映射。`CONTAINER_UP_API_KEY` 仅用于上游网关需要 Bearer 凭证时；默认留空。

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
  "url": "https://files.example.com/nanobot-attachments/hazard-expert/...?X-Amz-Signature=...",
  "filename": "现场照片.jpg",
  "content_type": "image/jpeg",
  "size_bytes": 123456,
  "bucket": "nanobot-attachments",
  "object_key": "hazard-expert/<用户ID哈希>/2026/09/21/<随机ID>-现场照片.jpg",
  "expires_at": "2026-09-22T08:00:00+00:00"
}
```

下载链接默认有效 24 小时（最多 7 天），无需公开桶。到期后持久化对象仍存在；本服务没有额外注册续签或删除工具。上传不会覆盖同名对象。
历史会话重读附件仍受链接有效期约束，需由 nanobot 保留已下载附件，或重新上传并提供新的链接。

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
URL 会保留完整查询字符串，支持预签名链接；nanobot 所在环境必须能访问这些 URL。

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

会话保存在后端 nanobot。本服务不把 MCP 连接 ID 当作聊天 ID，也不在重新连接或重启时生成新的 chat_id。
**后端 `agents.defaults.unifiedSession`（Python 配置字段 `unified_session`）须为 `false`**，否则 nanobot 会合并不同 chat_id；当前默认值为 false。同一会话应在上一轮完成后再发下一轮。

## 超时和运行约束

调用方超时或断开后，应使用相同 `user_id` 和 `chat_id` 调用 `query_analysis_result`，不要再次提交分析：

```json
{"user_id": "user-123", "chat_id": "inspection-20260921-001"}
```

返回 `status`、`records` 和 `next_before_id`。`records` 默认包含最近 20 轮，最新在前；每轮保存 `request_id`、`query`、附件 URL、时间、`status`、`result` 和 `error`。确认问题和提交时间对应目标轮次，再读取 `result.content`，避免把旧轮答案当作新轮结果。可传 `limit`（1–100）；有 `next_before_id` 时将其作为 `before_id` 获取更早记录，此时顶层状态表示该页最新一条的状态。

- `running`：仍在处理，同一用户和会话的结果轮询至少间隔 15 秒，禁止连续或并发轮询；其他状态停止自动轮询。
- `completed`：最终结果已保存，读取 `result`。
- `unknown`：上游请求超时、返回错误或 MCP 中断，尚未取得最终结果；需核查后端，不能保证之后查询能拿到答案。
- `not_found`：该用户和会话没有本地记录；仅记录通过此 MCP 提交的分析。

调用方连接取消不会取消独立的后台转发任务；只要 MCP 进程仍运行、上游请求仍有效，收到答案后会写入 SQLite。MCP 到后端本身超时或后端返回 504 时，本服务没有后端结果恢复接口，无法获取后续答案。相同会话正在处理时，相同问题和附件的重复调用复用同一任务；不同问题会被拒绝，需等待本轮结束。这不提供跨轮次的幂等重试保证。

`HISTORY_DB_PATH` 指定 SQLite 路径。Compose 使用 `hazard-history` 命名卷；Kubernetes 新增独立的 `hazard-expert-history-pvc`（1 GiB，需要默认 StorageClass 或手工绑定 PV）。已保存结果在容器重建后保留，服务重启时将遗留的 `running` 标记为 `unknown`，不会自动重发。部署必须保持单副本、单进程；Kubernetes 使用 Recreate 更新策略。记录暂不自动清理，请按实际容量维护备份和存储。调用方仍须可信地绑定当前登录用户 ID。

- `CONTAINER_UP_TIMEOUT_SECONDS` 默认 720 秒，覆盖上游默认 600 秒同步等待及实例冷启动。MCP 客户端、反向代理的请求/读取超时也需要相应调大。
- 上游请求不自动重试。超时、断网或 MCP 客户端断开并不保证后端任务被取消；避免自动重复提交造成重复执行。
- 当前 container-up 的 `SyncReplyBroker` 使用进程内存。请求和对应 outbound 回调必须到达同一 container-up 进程；部署多副本/多 worker 前需处理回复路由或共享关联状态。MCP 本身无状态不能消除这一限制。
- `/healthz` 只检查 MCP 进程存活，不保证 container-up 或 MinIO 可用。

## 本地验证

```bash
python -m venv .venv
# Linux/macOS: .venv/bin/python；Windows: .venv/Scripts/python.exe
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests
```

测试通过 MCP HTTP JSON-RPC 验证三个工具、鉴权、请求限制，并验证上游参数转发、超时处理、MinIO 签名地址、断开后结果保存、用户隔离、分页及重启恢复。
测试使用模拟的 container-up 响应与 S3 Stubber，不依赖真实业务账号或对象存储凭证。

实现参考：[MCP Python SDK 1.26.0](https://github.com/modelcontextprotocol/python-sdk/tree/v1.26.0)、[Boto3 预签名下载链接](https://docs.aws.amazon.com/boto3/latest/guide/s3-presigned-urls.html)。
