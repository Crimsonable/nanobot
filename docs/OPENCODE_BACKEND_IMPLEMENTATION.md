# OpenCode 服务：配置、运行与部署

本项目仅使用 OpenCode server 作为 bucket 执行后端。

## 当前入口与边界

```text
渠道 / 业务前端
  ↕ HTTP / 平台订阅
container_up.server                 用户→bucket、渠道投递
  ↕ /internal/buckets/ws            双向控制 / 事件 / 附件引用
bucket_runtime.runtime              用户→conversation→session、状态、权限、附件发布
  ├─ OpenCodeClient ↔ opencode serve  回环 HTTP / SSE，一个 bucket 一个实例
  ├─ 可信插件 → 私有 runtime RPC       before hook / publish_attachment
  └─ SandboxExecutor → bwrap         被 hook 包装的 bash 在这里执行
```

container_up 不调用 OpenCode，不访问 bucket 文件系统，不接收模型指定的路由元数据。入站仅转发 URL/路径，不获取文件字节。publish_attachment 的 runtime 端实现持有 MinIO 凭据、校验本地文件并上传；凭据不交给模型或 bash 沙箱。WS 只传递消息、事件和附件引用。

新入口：

```bash
python -m agent_backend control --config /mnt/common/frontend.json

# runtime 由 CU 按需创建的 Deployment 启动，传入调度器生成的 --bucket-id。
```

两个后端 Dockerfile 使用上述入口，依赖通过 requirements-backend.lock 固定；服务配置来自 config/opencode 中的文件。

## 已落地的实现选择

- 构建与实测使用 OpenCode **1.18.31 / Linux x64**；运行时不限制版本号或二进制哈希，启动仅检查服务健康状态。构建产物的完整性校验见 vendor 脚本。
- 创建 session 时安装策略，每次复用时回读核对。实测该版本 PATCH 权限会追加规则，因此运行时不反复 PATCH；策略变更需要迁移到新 session。
- server 配置直接读取 `opencode.config_file` 指向的完整 OpenCode JSON，仅追加 runtime 插件；不再覆盖功能开关或注入禁用功能的环境变量。
- 用户权限在 `user-permissions.yaml` 中按 frontend_id + user_id 匹配。sandbox 和 session 各有独立开关；用户字段覆盖 defaults，未配置字段继承。工具默认可用，目录权限从 filesystem 自动编译，不再通过一个全局通配符放行所有路径。
- bwrap 按该用户的工作目录模式和公共目录清单挂载；原生 read/edit/write/glob/grep/list/lsp/multiedit/apply_patch 在 hook 中检查、规范化目标路径，并叠加 session 路径权限。该检查不能消除主机侧 TOCTOU，也不覆盖 MCP、LSP 后台进程、技能内部读写或自定义工具的任意 I/O；这些能力不具备 bwrap 的 OS 隔离保证。
- 启用 bwrap 时仅挂载当前用户允许的目录、公共只读根及基础运行库；network 控制网络 namespace，PID namespace 和干净环境保留。初始化失败拒绝执行，不自动降级。只有管理员显式设置 sandbox.enabled=false 时才使用无 bwrap 的 shell。
- 单用户请求串行，文件操作另有互斥；不同用户并发上限由 `limits.active` 控制。执行票据绑定 session/call/request，重复执行返回已有结果，不再次启动命令。
- 动态池默认恢复原来的 frontend_id/user_id 目录布局（runtime.workspace_layout=frontend_user），所有 bucket 挂载同一服务器数据根。数据库记录目录归属，拒绝旧清洗规则产生的身份碰撞。用户映射只在 bucket 存活期间有效；conversation 按用户和 chat_id 分开。identity_hash 布局仍可用于新环境。
- CU 与每个 bucket 使用独立 SQLite。状态和 outbox 原子提交，ACK 后删除待发送记录，inbox 去重；数据库文件有进程级单写锁。断线重放消息，重复 request_id 校验请求 hash。
- WS 每个已登记的连接绑定一个 bucket，新连接关闭旧连接，旧 handler 拒绝继续处理。bucket 回收后释放用户映射，下次重新分配；带未完成任务时不迁移。
- 一个接收循环分发消息和 RPC；不再提供 input.fetch、input.begin/chunk/commit 或 attachment.begin/chunk/commit 文件传输。
- SSE 事件经过 session 映射后转为进度/正文快照；终态通过该轮 input message ID 对应的 assistant 快照核实。工具消息和 idle 事件不提前结束同步请求。
- 取消会终止沙箱进程树，已完成终态不可覆盖。提交结果不确定或进程崩溃时查询已持久化消息；不能确认结果就返回 `EXECUTION_INTERRUPTED`，不自动重跑有副作用的任务。
- 执行完成与渠道交付分开记录。外部发送状态不确定时记录 `unknown`，不盲目重试。

共享 OpenCode 主进程仍属于可信服务，并不拥有每用户独立的 OS 权限。OS 隔离覆盖的是 bwrap 执行的命令；用户不可直连 OpenCode/RPC 端口。这不是“共享主进程内核级多租户隔离”的承诺。

## 附件引用与发布

入站支持 `https://...` URL，以及 `{path: "/shared/file.pdf"}`、相对路径等引用；保持 URL/路径和文件名、MIME，图片保留 image_N 别名。CU/runtime 不下载、不 stat、不复制附件，不创建 inbox；引用作为消息文本传给 OpenCode，由模型按需调用工具。传入路径不授予访问权，实际读取仍须符合用户 session/hook 和 bwrap 配置。相对路径由文件工具按用户工作目录解释；绝对路径必须能在 bucket 中访问，bwrap 中还须有对应挂载。网络访问的 bash 命令仍受 sandbox.network 限制。

飞书原始 image_key/file_key 转为带 requires_auth 标记的资源 API URL，不自动下载。此类私有 URL 需要具备渠道授权的工具才能访问；既不视为公开链接，也不将渠道凭据发送给模型。

出站由 OpenCode 主动调用 `publish_attachment(files=[{path, filename?}])`。runtime 验证会话、用户、工具开关、票据、路径和文件大小；仅接受用户工作目录内的普通非链接文件，生成本地快照后直接上传 MinIO，取得对象 URL。幂等记录保存在 bucket 数据库，重复调用不重复上传。存储失败会返回工具错误，不登记成功 URL。

上传成功后，工具返回对象 URL，runtime 将附件元数据加入本轮结果，经 WS 发给 CU。Web 返回正文和 attachments；QXT/飞书将对象 URL 作为文本链接发送，不下载再上传到渠道。工具成功代表对象已上传并加入待回复附件，最终渠道投递状态由 CU 记录。

MinIO 配置统一位于 frontend.json 的 attachment_storage，支持原有 access_key、secret_key、key_prefix、presign_expiry_seconds 字段。bucket 内的发布工具读取它并上传；CU 仅转发 URL。public_base_url 为空时返回签名 URL，CU 不续签。

`public/skills/` 提供报告、HTML→PDF、图片压缩及附件发布工具说明。模型可自行获取需要的资源，再把本地路径交给报告脚本；报告脚本不自动下载远程图片。

## 配置与部署

配置示例位于 `config/opencode/`。CU 与 runtime 共同读取同一份 `frontend.json`，不再维护单独的 runtime 配置或渠道回调 JSON。顶层及 control/runtime 字段使用严格 schema；渠道扩展放在 channel 对象中。模型凭据仍由 OpenCode 原生配置引用。

配置位置：

- `config/opencode/frontend.json`：保留 id、provider、send_msg_url、attachment_storage、instance_idle_ttl_seconds。control 配置 CU 监听、状态目录、bucket_pool 动态池参数；runtime 配置 WS 地址、端口、工作目录、模型 profile、bwrap 基础挂载及下列两个文件的路径。profile_id 选择该 frontend 使用的 profile；api_key_file 配置入站认证。飞书 app_id/app_secret 等渠道专有配置放入 channel。单份文件对应一个 frontend 及其 bucket 集群。
- `config/opencode/opencode-server.json`：完整 OpenCode 配置（provider、插件、MCP、LSP、formatter 等）；自动更新、snapshot、自动压缩已开启，分享采用 manual。空的 MCP 配置表示尚未配置服务，并非禁用 MCP。模型密钥由 `/run/secrets/opencode-api-key` 读取。
- `config/opencode/user-permissions.yaml`：默认权限与逐用户覆盖，详见下面的开关和目录配置。重启 runtime 加载配置；已有 session 的规则不自动 PATCH，修改 session 策略后需使用新 conversation。

### 用户权限开关和目录范围

```yaml
defaults:
  sandbox:
    enabled: true
    network: false
  session:
    enabled: true
    default_tools: true
    tools: {webfetch: false}
  filesystem:
    workspace: read_write
    public_roots: null
users:
  - frontend_id: web-yk
    user_id: alice
    filesystem:
      workspace: read_only
      public_roots: [/opt/public/manuals]
    session:
      tools: {bash: true, task: false}
```

- `filesystem.workspace`：`read_write` / `read_only` / `none`。目录始终由 runtime 按用户身份映射，不允许把其他用户的目录作为自己的工作目录。
- `filesystem.public_roots`：公共只读目录的完整清单；`null` 继承当前 profile 的公共目录，`[]` 表示不授予任何公共目录。用户配置清单替换默认清单，不叠加；服务状态、凭据和整个用户根目录不能配置为公共目录。
- `sandbox.enabled`：是否将该用户的 bash 放入 bwrap；启用时工作目录分别使用读写挂载、只读挂载或不挂载。基础运行库仍由 frontend.json 的 runtime.sandbox.readonly_roots 提供，它们不是用户公共资料目录。关闭后 shell 使用 server 的 OS 文件权限，filesystem 不能限制 shell。
- `sandbox.network`：bwrap 是否共享宿主网络；只在 sandbox.enabled=true 时生效。
- `session.enabled`：是否安装用户 session 权限及启用 native 工具路径 hook；关闭后 native 工具按 server 配置处理，bwrap 开关保持独立。
- `session.default_tools` 和 `session.tools`：默认工具开关及逐工具 bool 覆盖。工具开关不扩大目录范围；即使 write=true，也不能写公共只读目录。OpenCode 的 write/apply_patch 共用 edit 权限，因此 runtime 编译公共 edit 路径规则，hook 再按实际工具名检查开关。

两层同时开启时，同一份 filesystem 范围同时作用于 bwrap 和 session/hook。相对文件路径按用户工作目录解释。read/edit 的权限规则按 OpenCode 实际 worktree 生成；glob/grep 的搜索目录额外由 hook 检查，因为其原生权限匹配的是搜索表达式。参考：[OpenCode permissions](https://opencode.ai/docs/permissions/)。

部署时将 frontend.json 复制到服务器 common 目录，将另外两个文件放入 `agent-config` ConfigMap。两种服务都挂载同一份 frontend.json。MinIO 支持 access_key/secret_key 直接配置，也支持改用 access_key_file/secret_key_file 引用 Secret 文件，每个凭据二选一。OpenCode 其余功能恢复自身默认行为，runtime 不再硬编码关闭。配置字段参考 https://opencode.ai/docs/config/ 。

用户发送独立的 `/cancel`（允许首尾空白，忽略大小写）时，runtime 在排队前处理，只取消相同 frontend/user/conversation 的活动及排队请求。活动会话调用 `POST /session/{sessionID}/abort`，停止沙箱执行；取消消息本身返回确认，不提交给模型。重放同一个 request_id 不重复停止，无活动请求时返回提示。接口参考 https://opencode.ai/docs/server/ 。

先检查 schema：

```bash
python -m agent_backend check --config config/opencode/frontend.json
```

schema 检查不代表凭据存在、模型可用或 bwrap 已通过启动测试。

连接公网的 Linux x64 构建机：

```bash
bash scripts/vendor_opencode_backend.sh
docker build -f Dockerfile.container_up -t agent-container-up:opencode-1.18.31 .
docker build -f Dockerfile.bucket_runtime -t agent-bucket-runtime:opencode-1.18.31 .
docker save agent-container-up:opencode-1.18.31 agent-bucket-runtime:opencode-1.18.31 -o agent-images.tar
```

APT、Chromium 和基础镜像获取发生在构建期；离线环境导入完成的镜像。不是在无基础依赖的机器上直接执行联网 Docker 构建。Python 包由带 hash 的 lock 文件和 wheelhouse 安装，插件由 npm lock 构建并包含离线依赖，runtime 不主动安装依赖；开启的 OpenCode 自动更新、插件或 LSP 下载功能可能自行访问网络，离线部署需预置所需资源。

## 动态 bucket 池（恢复原有调度机制）

参考迁移前 ae37e6c1 中的 binding_repository、bucket_scheduler、bucket_manager：数据库事务预占名额、复用未满 bucket、按需创建 Deployment、空闲后 scale 到 0。旧实现的每用户进程生命周期不再保留；现在一个 bucket 只有一个 runtime 和一个 OpenCode server，多个用户各自使用 session。

1. 全新部署只有 CU（以及 Service/RBAC/Secret 等资源），没有 bucket Pod。
2. CU 在 SQLite 的 bucket/binding 记录中原子预占用户容量。已分配用户复用当前 bucket；新用户优先选择有容量的活跃 bucket，没有则创建或唤醒空 bucket。
3. max_users_per_bucket 限制整个 bucket 存活期间分配的不同用户数；runtime.limits.active 另行限制同时执行的请求数，两者不是同一个限制。不会按单个用户空闲释放名额。
4. CU 调用 Kubernetes API 创建 Deployment，runtime 启动唯一的 OpenCode server、完成插件初始化并连接 CU 的 WS 后才派发请求。冷启动失败回滚未接受请求的用户预占。
5. 所有任务完成后开始 bucket 闲置计时。instance_idle_ttl_seconds 达到后整体 scale 到 0，等待 Pod 退出，再释放该 bucket 的全部映射。0 禁用自动回收。存在排队/执行中请求、启动预占或未 ACK 命令时不回收。回收失败保留 draining 状态并重试。
6. Deployment 对象及服务器数据保留，Pod 被销毁；之后可重新唤醒。CU 重启从数据库恢复映射和请求，不把未完成请求转到其他 bucket。

K8s 示例：k8s/opencode/backend.yaml。CU 的 ServiceAccount 只授予当前命名空间内 Deployment 创建/查询/更新、scale 查询/更新和 Pod 查询。runtime 不挂载 ServiceAccount token。CU/runtime WS 不使用通信认证密钥；CU 仍检查 bucket ID 已登记且处于启动/运行状态，并校验请求归属。

frontend.json 的 control.bucket_pool 配置镜像、命名空间、max_users_per_bucket、启动超时、扫描间隔、PVC 名称与节点选择器。两种服务挂载同一份 frontend.json，不生成独立 runtime 配置。配置更改需要重启 CU，并在 bucket 重建时生效。

服务器目录沿用原 k8s/base/base.yaml 的 PV/PVC 机制：data PVC → /mnt/env，source PVC → /mnt/source（只读），common PVC → /mnt/common（只读）。物理目录仍是 /data/nanobot_backend_k8s/{data,source,common}，用户工作目录仍是 /mnt/env/workspaces/<frontend>/<user>，不需要搬迁已有文件。storage.yaml 通过 storage_backend 选择 local 或 NFS；服务 SQLite/OpenCode 状态另用本地 state PVC → /data。每次收到请求和真正开始执行前均检查工作目录，缺失时重建，并补齐 output/scratch；可选模板来自 /mnt/source/templates，不覆盖已有文件。公共目录同时列入用户默认权限和 bwrap 只读挂载。

完整部署步骤、节点权限、Secret、NodePort 与 local/NFS 切换见 [Kubernetes 部署说明](../k8s/opencode/README.md)。

用户重新分配后工作文件直接复用，无需复制。新的分配周期建立新的 OpenCode session；CU 将最近最多 32 轮、128 KB 的历史正文和附件引用作为历史上下文交给新 session，完整结果仍保存在 CU 数据库。此方式恢复对话上下文，不迁移 OpenCode 内部工具调用树或分享状态。

部署前创建以下资源（命名空间须与 bucket_pool.namespace 一致）：

```bash
kubectl apply -f k8s/opencode/storage.yaml
cp config/opencode/frontend.json /data/nanobot_backend_k8s/common/frontend.json
kubectl -n opencode-sv create configmap agent-config --from-file=config/opencode/opencode-server.json --from-file=config/opencode/user-permissions.yaml
# agent-control-secrets：web-api-key
# agent-runtime-secrets：opencode-api-key
kubectl apply -f k8s/opencode/backend.yaml
```

现有 bwrap 节点要求不变：允许非特权 user namespace，并配置兼容的 seccomp/AppArmor 策略。附件仅传引用；publish_attachment 继续在 bucket 内上传 MinIO，CU 不下载文件。
