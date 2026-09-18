# OpenCode 后端：实现、运行与迁移

本分支从 `ae37e6c1` 建立，替换 bucket 的 agent 执行链路。原工作区的上游 merge 冲突没有参与这次实现。

## 当前入口与边界

```text
渠道 / 业务前端
  ↕ HTTP / 平台订阅
container_up.server                 用户→bucket、存储、渠道投递
  ↕ /internal/buckets/ws            双向控制 / 事件 / 二进制附件
bucket_runtime.runtime              用户→conversation→session、状态、权限、附件汇总
  ├─ OpenCodeClient ↔ opencode serve  回环 HTTP / SSE，一个 bucket 一个实例
  ├─ 可信插件 → 私有 runtime RPC       before hook / publish_attachment
  └─ SandboxExecutor → bwrap         用户命令仅在这里执行
```

container_up 不调用 OpenCode，不访问 bucket 文件系统，不接收模型指定的路由元数据。插件和工具不持有 container_up/MinIO/渠道凭据。runtime 对工具上传进行归属、路径与快照校验，再经 WS 传输给 container_up。

新入口：

```bash
python -m agent_backend control --config /etc/agent/container-up.yaml
python -m agent_backend runtime --config /etc/agent/bucket-runtime.yaml
```

两个 Dockerfile 已切换为上述入口。旧 `container_up.app`、`bucket_runtime.main/local_service/process_manager` 和 `nanobot/` 保留供旧镜像回滚、历史代码对照；新服务不导入它们，也不使用旧 `settings.py/config.py` 的环境变量。不要用旧 K8s 配置启动新镜像。

## 已落地的实现选择

- 固定 OpenCode **1.18.31 / Linux x64**，启动校验解压后二进制 SHA-256；归档校验和见 vendor 脚本。
- 创建 session 时安装策略，每次复用时回读核对。实测该版本 PATCH 权限会追加规则，因此运行时不反复 PATCH；策略变更需要迁移到新 session。
- server 使用一个空的可信 context 目录，用户工作目录由 runtime 提供给执行器。关闭用户项目配置、外部技能发现、默认插件、MCP/LSP/格式化、自动更新和模型列表获取。
- native read/edit/glob/grep/task 等暂不开放。session 规则默认拒绝所有工具，只开放被 hook 包装的 bash 和附件工具；未知工具仍由 hook 拒绝。通过 bash 可读取用户文件、列目录、搜索、运行公共技能脚本。
- bwrap 从受限挂载视图启动，仅挂载当前用户目录、允许的公共读根和运行库；独立网络/PID namespace、干净环境。初始化失败直接拒绝就绪，没有裸执行降级。
- 单用户请求串行，文件操作另有互斥；不同用户并发上限由 `limits.active` 控制。执行票据绑定 session/call/request，重复执行返回已有结果，不再次启动命令。
- 每用户工作目录使用完整身份摘要，不再使用会碰撞的字符替换。用户绑定 bucket 后不自动漂移；conversation 按用户和 chat_id 分开。
- CU 与每个 bucket 使用独立 SQLite。状态和 outbox 原子提交，ACK 后删除待发送记录，inbox 去重；数据库文件有进程级单写锁。断线重放消息，重复 request_id 校验请求 hash。
- WS 每个认证连接绑定一个 bucket，新连接关闭旧连接，旧 handler 拒绝继续处理。当前为固定 bucket 归属，不支持带未完成任务的跨 bucket 热迁移。
- 一个接收循环分发独立 RPC 任务，上传 RPC 可以反向请求另一侧；文件为 256 KiB 二进制块，按持久化 offset 恢复，commit 验证 SHA-256 和大小。
- SSE 事件经过 session 映射后转为进度/正文快照；终态通过该轮 input message ID 对应的 assistant 快照核实。工具消息和 idle 事件不提前结束同步请求。
- 取消会终止沙箱进程树，已完成终态不可覆盖。提交结果不确定或进程崩溃时查询已持久化消息；不能确认结果就返回 `EXECUTION_INTERRUPTED`，不自动重跑有副作用的任务。
- 执行完成与渠道交付分开记录。外部发送状态不确定时记录 `unknown`，不盲目重试。

共享 OpenCode 主进程仍属于可信服务，并不拥有每用户独立的 OS 权限。OS 隔离覆盖的是 bwrap 执行的命令；用户不可直连 OpenCode/RPC 端口。这不是“共享主进程内核级多租户隔离”的承诺。

## 附件与公共技能

入站保留 URL / MIME / filename 语义和图片 `image_N` 别名。CU 在配置的下载主机与地址范围内获取文件，经 WS 预取到该用户 inbox。QXT URL 由新的 ArtifactService 统一下载，避免旧辅助函数绕过地址校验；Feishu SDK 下载的本地文件先在 CU 完成标准化和对象存储上传。不再映射 bucket 的宿主/容器双路径。

出站调用：

```json
{"files":[{"path":"output/report.pdf","filename":"报告.pdf"}]}
```

工具不接受 user_id、收件地址或上传端点。文件经快照、幂等 upload_id、MinIO 上传后登记到当前请求。最终正文与本轮全部附件一起返回。上传成功的工具回复是 `queued_for_reply`，不是渠道已收到。

`public/skills/` 提供迁移后的报告生成、HTML→PDF、图片压缩与发布说明。报告脚本保留原生成逻辑，去掉内置上传地址、上传配置和网络下载；图片使用预取的本地路径并内嵌到报告。原脚本引用的部分规范知识库不在本仓库，需要部署方提供，不能据此生成虚构条款。

Python 文档/图片工具和 Playwright/Chromium 在构建镜像时安装。沙箱中的脚本没有网络权限，依赖或远程资源缺失时应修正镜像/预取配置，不在运行时安装。

## 配置与部署

配置示例位于 `config/opencode/`，所有字段使用严格 schema；未知字段直接报错。新服务的配置来源是显式 YAML 和凭据文件，不读取旧环境覆盖值。模型密钥仅在 bucket 的模型配置文件；IM/MinIO 密钥仅在 control 服务。

先检查 schema：

```bash
python -m agent_backend check --config config/opencode/container-up.yaml
python -m agent_backend check --config config/opencode/bucket-runtime.yaml
```

schema 检查不代表凭据存在、模型可用或 bwrap 已通过启动测试。

连接公网的 Linux x64 构建机：

```bash
bash scripts/vendor_opencode_backend.sh
docker build -f Dockerfile.container_up -t agent-container-up:opencode-1.18.31 .
docker build -f Dockerfile.bucket_runtime -t agent-bucket-runtime:opencode-1.18.31 .
docker save agent-container-up:opencode-1.18.31 agent-bucket-runtime:opencode-1.18.31 -o agent-images.tar
```

APT、Chromium 和基础镜像获取发生在构建期；离线环境导入完成的镜像。不是在无基础依赖的机器上直接执行联网 Docker 构建。Python 包由带 hash 的 lock 文件和 wheelhouse 安装，插件由 npm lock 构建并包含离线依赖，启动不执行公网安装。

K8s 示例：`k8s/opencode/backend.yaml`。需要创建 `agent-config` ConfigMap、`agent-control-secrets`、`agent-bucket-0-secrets`，以及公共知识库 PVC。示例为单 CU、单 bucket；扩容时每个 bucket 必须具有独立配置、凭据、ID 与数据卷，并在 CU 登记。不能仅把同一份 bucket-0 配置的 replicas 改为 2。

如果不使用公共 PVC，移除 `/opt/public` 的卷挂载即可使用镜像内置技能。使用 PVC 时先复制/合并 `public/` 内容，空卷会遮盖镜像内的技能。

节点需允许 bwrap 使用的非特权 user namespace / mount / PID namespace。示例 RuntimeDefault seccomp 是否允许取决于集群；需要管理员提供经过验证的节点策略。不要用 privileged 或关闭沙箱兜底。

MinIO 主机必须出现在 `download_hosts` 中；内网地址需在 `download_private_cidrs` 精确放行。HTTP 下载检查重定向和解析地址，TLS 默认验证证书。

当前调度在预部署的 bucket 池中进行。自动创建/缩容 bucket 的旧 kubectl 命令模板不进入新启动路径；生命周期交由 K8s 管理，缩容前必须停接请求并保留 PVC。资源示例不是容量结论，生产并发需实测。

## 外部接口与兼容

业务 HTTP 接口新增 `Authorization: Bearer <frontend-api-key>`，由可信渠道网关调用。令牌授权的是该 frontend，终端用户身份应由网关认证后填入，不能把此令牌直接分发给普通用户。

| 接口 | 行为 |
|---|---|
| POST `/inbound/{frontend_id}` | 提交并返回 request_id、bucket、conversation |
| POST `/inbound-sync/{frontend_id}` | 等本轮完整终态，返回正文、附件或明确失败 |
| GET `/requests/{frontend_id}/{request_id}` | 查询状态、正文进度快照、最终结果与交付状态 |
| POST `/requests/{frontend_id}/{request_id}/cancel` | 取消指定请求 |
| POST `/api/message` / `/api/cancel` | 兼容 frontend_id + usr_id 形式 |
| POST `/internal/attachments/upload` | 保留 multipart 上传和 JSON URL 标准化，需 frontend 凭据 |
| POST `/subscribe/{frontend_id}` | QXT 原签名回调；Feishu 继续使用其订阅适配 |

`instance_id` 暂作为用户绑定 ID 的兼容字段，不再代表进程。HTTP 同步连接无法跨 CU 重启复活；客户端使用相同 request_id 重试或查询。旧 `/outbound`、bridge、runtime-release 和 debug 入口不在新服务开放。

## 迁移与回滚

1. 用 `scripts/audit_backend_env.py old.env --output env-audit.json` 盘点旧变量。报告只有变量名与去向，不输出值；未知/重复变量需明确处理。
2. 在新镜像中配置模型、渠道、存储和 public profile。不要复制旧源码 PVC、bridge 端点、端口池或 `NANOBOT_*` 环境。
3. 停止旧实例接单并等待任务完成，备份旧业务绑定、工作区和历史。准备显式迁移 manifest：

```json
[{"frontend_id":"web","user_id":"alice","bucket_id":"bucket-0","source_workspace":"/old/web/alice"}]
```

```bash
python -m scripts.migrate_backend_workspaces --manifest users.json \
  --source-root /old --workspace-root /new/bucket-0/workspaces --control-db /new/control/control.sqlite
# 核对 dry-run 后，使用相同参数加 --apply。
```

脚本只复制，不删除旧文件；拒绝路径越界、符号链接、重复身份和覆盖已有目标。迁移时 CU 必须停止，单写锁会拒绝并发修改。按 bucket 分开提供 manifest/目标卷。

4. 切换单个 frontend/用户流量验证，禁止新旧后端同时执行同一请求。旧 nanobot 对话 JSONL 不写入 OpenCode DB；保留用于历史查询，必要时人工确认摘要后开启新会话。
5. 验证成功后扩大流量。回滚时先停止新任务并备份新卷，再切回旧镜像/旧配置；不删除新工作区。两种后端的对话状态不能无损互转。

未发现 container_up/bucket_runtime 接入 cron、heartbeat、nanobot WebUI 或 OpenAI-compatible API 的业务路由。本分支保留其旧代码，不声称 OpenCode 实现了这些 nanobot 功能。若有调用方绕过 CU 直接使用它们，应继续留在旧部署，单独迁移。

## 验证与生产边界

自动测试：

```bash
python -m pytest tests/backend -q
cd opencode_plugin && npm ci && npm run build && npm test
```

真实 OpenCode / bwrap 测试需要 Linux：

```bash
OPENCODE_TEST_BINARY=/path/to/verified/opencode python -m pytest tests/backend -q
```

集成测试使用本地 OpenAI-compatible 模拟服务，不调用商业模型。它实际运行固定二进制、插件、HTTP/SSE、双向 WS 和 bwrap，验证入站文件、报告文件生成/发布、同步完整回复、幂等、不同用户 session、取消与目录隔离。对象存储与外部 IM 投递使用测试替身；真实 MinIO、渠道账号、集群镜像构建与长时间容量测试仍须在部署环境验收。

另外测试：journal 原子提交/单写锁、断线消息重放所需的持久化、同连接反向 RPC、文件 offset/checksum/归属、未知工具拒绝、票据防重执行、路径/链接拒绝、公共目录只读、环境清理、后台进程清理、超时与输出上限、本地报告生成和迁移不覆盖。

不要把“单元测试通过”当作已完成生产灰度。本次没有使用生产凭据、启动真实渠道投递、操作集群或执行旧数据迁移。

2026-09-18 本地验证记录：Linux/WSL 下 `tests/backend` 加附件与渠道相关四组回归共 **38 passed**，包含真实 OpenCode 1.18.31 和 bwrap；插件 Node 测试通过；新增核心 Python 文件 Ruff 检查通过，示例配置 schema 与 K8s YAML 解析通过。Docker 镜像构建和真实外部服务验收不包含在这项结果中。
