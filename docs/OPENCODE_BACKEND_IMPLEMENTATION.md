# OpenCode 服务：配置、运行与部署

本项目仅使用 OpenCode server 作为 bucket 执行后端。

## 当前入口与边界

```text
渠道 / 业务前端
  ↕ HTTP / 平台订阅
container_up.server                 用户→bucket、渠道投递
  ↕ /internal/buckets/ws            双向控制 / 事件 / 附件引用
bucket_runtime.runtime              用户→chat_id→session、状态、权限、附件发布
  ├─ OpenCodeClient → opencode serve  回环 HTTP 提交/控制，一个 bucket 一个实例
  ├─ OpenCode 插件 → runtime → CU  全量事件与最终回复回调
  ├─ 可信插件 → 私有 runtime RPC       before hook / publish_attachment
  └─ SandboxExecutor → bwrap         被 hook 包装的 bash 在这里执行
```

container_up 不直接调用 OpenCode HTTP API，也不接收模型指定的路由元数据。会话换桶时，CU 通过共享本地 state PVC 读取和迁移原生会话数据库；在线旧桶的停止与删除操作交由 runtime 调用 OpenCode。入站附件仅转发 URL/路径，不获取文件字节。publish_attachment 的 runtime 端实现持有 MinIO 凭据、校验本地文件并上传；凭据不交给模型或 bash 沙箱。WS 传递消息、事件、附件引用及少量会话迁移控制，不传历史正文。

两个组件各自提供启动入口，`agent_backend` 仅提供配置、协议和持久化等公共库：

```bash
python -m container_up --config /mnt/common/frontends.json

# runtime 由 CU 按需创建的 Deployment 启动，传入调度器生成的 --bucket-id 和所属 --frontend-id。
python -m bucket_runtime --config /mnt/common/frontends.json --bucket-id "$BUCKET_ID" --frontend-id "$FRONTEND_ID"
```

两个后端 Dockerfile 使用上述入口，依赖通过 requirements-backend.lock 固定；全部业务源码、插件及插件依赖由同一个 source PVC 挂载到 /mnt/source；业务资料、脚本和 skills 则通过各 frontend 的 common/app 挂载。所有桶只读挂载服务器 opencode 目录到 /opt/opencode，共用其中的 opencode 二进制。服务配置来自服务器挂载目录。

## 已落地的实现选择

- 构建与实测使用 OpenCode **1.18.31 / Linux x64**；运行时不限制版本号或二进制哈希，启动仅检查服务健康状态。构建产物的完整性校验见 vendor 脚本。
- 创建 session 时安装策略，每次复用时回读核对。实测该版本 PATCH 权限会追加规则，因此运行时不反复 PATCH；策略不匹配时明确报错，需管理员明确处理会话策略。
- server 配置直接读取 `opencode.config_file` 指向的完整 OpenCode JSON，仅追加 runtime 插件；不再覆盖功能开关或注入禁用功能的环境变量。
- 用户权限在 `user-permissions.yaml` 中按 frontend_id + user_id 匹配。frontend 桶要求 sandbox.enabled 和 session.enabled 均为 true，禁止通过关闭隔离绕过目录边界；用户字段覆盖 defaults，未配置字段继承。工具默认可用，目录权限从 filesystem 自动编译，不再通过一个全局通配符放行所有路径。
- bwrap 按该用户的工作目录模式和公共目录清单挂载；原生 read/edit/write/glob/grep/list/lsp/multiedit/apply_patch 在 hook 中检查、规范化目标路径，并叠加 session 路径权限。该检查不能消除主机侧 TOCTOU，也不覆盖 MCP、LSP 后台进程、技能内部读写或自定义工具的任意 I/O；这些能力不具备 bwrap 的 OS 隔离保证。
- 启用 bwrap 时仅挂载当前用户允许的目录、公共只读根及基础运行库；network 控制网络 namespace，PID namespace 和干净环境保留。初始化失败拒绝执行，不自动降级。frontend 桶拒绝 sandbox.enabled=false。
- 单用户请求串行，文件操作另有互斥；不同用户并发上限由 `limits.active` 控制。执行票据绑定 session/call/request，重复执行返回已有结果，不再次启动命令。
- 动态池先按 frontend 分桶，再使用 frontend_id/user_id 目录布局，每个 bucket 只挂载自己 frontend 的用户目录子树。frontend ID 原样校验使用；数据库记录用户目录归属，拒绝 user_id 清洗规则产生的身份碰撞。用户只在有未完成请求或分配中的请求时占用桶容量；下一次空闲后的分配只取决于活跃桶容量。会话位置由 CU 按 frontend、用户和 chat_id 独立持久化；换桶时原生历史经 CU 内存迁移，保持 session_id，删除旧桶副本，详见 SESSION_LOGGING.md。
- CU 与每个 bucket 使用独立 SQLite。状态和 outbox 原子提交，ACK 后删除待发送记录，inbox 去重；数据库文件有进程级单写锁。断线重放消息，重复 request_id 校验请求 hash。
- WS 每个已登记的连接绑定一个 bucket，新连接关闭旧连接，旧 handler 拒绝继续处理。执行中及排队请求维持临时用户绑定，CU 重启后恢复其原桶；空闲用户不保留桶绑定。
- WebSocket 传业务事件、ACK 和内存中的会话停止/删除控制；历史由 CU 读取共享 state PVC，不经 WS 传输。业务事件发送由 outbox 入队唤醒，断线重连重发未确认事件。
- 事件只走 OpenCode 插件 → runtime → CU。插件通过带鉴权的 /plugin/event 将 event hook 收到的全部原始事件逐条推送，包括思考、正文增量、工具、会话状态、未知类型及终态之后的事件；工具执行前还推送原始参数。runtime 将这些事件写入可靠 outbox，CU 完整保存为 opencode_event 记录。原始事件不节流、不截断，不因缺少 request_id 而丢弃；确定请求归属时核对 session_id、input_message_id、directory 并附加可信用户与 chat_id。runtime 从同一原生事件流汇总最终助手正文、附件和用量，CU 仅根据最终结果向渠道回复，思考和工具中间内容不进入渠道消息。工具消息和 idle 不提前结束请求。runtime 不订阅 SSE，不轮询或补查消息；提交连接结果不确定时只等待插件回调至截止时间，重启前未持久化终态的执行中请求标记 EXECUTION_INTERRUPTED。
- 取消会终止沙箱进程树，已完成终态不可覆盖。提交结果不确定时等待插件事件；进程重启后将未完成的执行标记为 `EXECUTION_INTERRUPTED`，不自动重跑有副作用的任务。
- 执行完成与渠道交付分开记录。外部发送状态不确定时记录 `unknown`，不盲目重试。

共享 OpenCode 主进程仍属于可信服务，并不拥有每用户独立的 OS 权限。OS 隔离覆盖的是 bwrap 执行的命令；用户不可直连 OpenCode/RPC 端口。这不是“共享主进程内核级多租户隔离”的承诺。

## 附件引用与发布

入站支持 `https://...` URL，以及 `{path: "/shared/file.pdf"}`、相对路径等引用；保持 URL/路径和文件名、MIME，图片保留 image_N 别名。CU/runtime 不下载、不 stat、不复制附件，不创建 inbox；引用作为消息文本传给 OpenCode，由模型按需调用工具。传入路径不授予访问权，实际读取仍须符合用户 session/hook 和 bwrap 配置。相对路径由文件工具按用户工作目录解释；绝对路径必须能在 bucket 中访问，bwrap 中还须有对应挂载。网络访问的 bash 命令仍受 sandbox.network 限制。

飞书原始 image_key/file_key 转为带 requires_auth 标记的资源 API URL，不自动下载。此类私有 URL 需要具备渠道授权的工具才能访问；既不视为公开链接，也不将渠道凭据发送给模型。

出站由 OpenCode 主动调用 `publish_attachment(files=[{path, filename?}])`。runtime 验证会话、用户、工具开关、票据、路径和文件大小；仅接受用户工作目录内的普通非链接文件，生成本地快照后直接上传 MinIO，取得对象 URL。幂等记录保存在 bucket 数据库，重复调用不重复上传。存储失败会返回工具错误，不登记成功 URL。

上传成功后，工具返回对象 URL，runtime 将附件元数据加入本轮结果，经 WS 发给 CU。Web 返回正文和 attachments；QXT/飞书将对象 URL 作为文本链接发送，CU 不重新下载上传文件。工具成功代表对象已上传并加入待回复附件，最终渠道投递状态由 CU 记录。

MinIO 配置位于 frontends.json 中各 frontend 的 attachment_storage，使用 access_key、secret_key、key_prefix、presign_expiry_seconds 字段。bucket 内的发布工具读取它并上传；CU 仅转发 URL。public_base_url 为空时返回签名 URL，CU 不续签。

`config/opencode/web-yk/app/skills/` 提供报告、HTML→PDF、图片压缩及附件发布工具说明。模型可自行获取需要的资源，再把本地路径交给报告脚本；报告脚本不自动下载远程图片。

## 配置与部署

`common/frontends.json` 是统一入口总表，`frontends` 数组中每项定义一个 frontend，`control` 是 CU/集群的公共基础设施参数。一个 CU 接入多个 frontend，按 frontend 创建独立桶，不同 frontend 的模型、权限或模板不会混用。

- `config/opencode/frontends.json`：每项使用 frontend_id、provider、send_msg_url、attachment_storage、instance_idle_ttl_seconds、channel 和 runtime。模型通过该 frontend 的 opencode-server.json 的 model 选择，规则通过 instructions 指定的 AGENTS.md 加载；渠道凭据放在自己的 channel 中。飞书仅使用 app_id/app_secret；QXT 使用 app_id/app_secret/corp_id/callback_token/access_url，发送地址统一使用 frontend 的 send_msg_url。其他 channel 字段直接拒绝。
- `config/opencode/web-yk/config/opencode-server.json`：部署到 common/web-yk/config/，包含仅该 frontend 生效的模型 provider、API key、MCP、LSP、formatter、skills 等原生配置。自动更新关闭，二进制统一从服务器目录更新。
- `config/opencode/web-yk/config/user-permissions.yaml`：部署到 common/web-yk/config/，defaults 仅作用于该 frontend 的用户，users 可只写 user_id。显式 frontend_id 不匹配时启动失败。新 session 加载当前策略；已有 session 复用时核对策略，不匹配则明确报错，不自动重建会话或修改已有策略。
- `config/opencode/web-yk/config/session-logging.json`：部署到该 frontend 的 config/，由 runtime.logging_file 指定，控制插件向 bucket stdout 输出思考、回复和工具过程。身份来自 OpenCode 会话 metadata，详见 [日志与 ID 约定](SESSION_LOGGING.md)。
- `common/<id>/app/templates/` 与 `app/skills/`：该 frontend 自己的模板和技能，通过 common 挂载供所属 bucket 使用。

CU 的入站、查询、取消和附件引用接口不需要 API key；Web 代理及隐患专家调用 CU 不发送 Authorization。保留 frontend 注册校验和请求/bucket 归属检查。模型、MinIO、渠道凭据，以及桶内部自动生成的插件调用凭据仍属于各自用途。

### 用户权限开关和目录范围

```yaml
defaults:
  sandbox:
    enabled: true
    network: true
  session:
    enabled: true
    default_tools: true
    tools: {webfetch: false}
  filesystem:
    workspace: read_write
users:
  - user_id: alice
    filesystem:
      workspace: read_only
    session:
      tools: {bash: true, task: false}
```

- `filesystem.workspace`：`read_write` / `read_only` / `none`。目录始终由 runtime 按用户身份映射，不允许把其他用户的目录作为自己的工作目录。
- `filesystem.public_roots`：额外公共只读目录的清单；`null` 或 `[]` 均不授予额外目录。本 frontend 的整个 app 始终自动提供只读访问，无需另行配置。用户配置清单替换默认的额外清单；common 范围只能在 /mnt/common/<当前 frontend_id>/app 内，config、frontend 根、父目录和其他 frontend 均被拒绝。服务状态、凭据和整个用户根目录不能配置为公共目录。
- `sandbox.enabled`：是否将该用户的 bash 放入 bwrap；启用时工作目录分别使用读写挂载、只读挂载或不挂载。基础运行库仍由 frontends.json 中该 frontend 的 runtime.sandbox.readonly_roots 提供，它们不是用户公共资料目录。frontend 桶必须保持 true，默认或用户覆盖设置 false 都会拒绝启动。
- `sandbox.network`：bwrap 是否共享宿主网络；只在 sandbox.enabled=true 时生效。
- `session.enabled`：是否安装用户 session 权限及启用 native 工具路径 hook；frontend 桶必须保持 true，不能关闭目录检查。
- `session.default_tools` 和 `session.tools`：默认工具开关及逐工具 bool 覆盖。工具开关不扩大目录范围；即使 write=true，也不能写公共只读目录。OpenCode 的 write/apply_patch 共用 edit 权限，因此 runtime 编译公共 edit 路径规则，hook 再按实际工具名检查开关。

两层同时开启时，同一份 filesystem 范围同时作用于 bwrap 和 session/hook。相对文件路径按用户工作目录解释。read/edit 的权限规则按 OpenCode 实际 worktree 生成；glob/grep 的搜索目录额外由 hook 检查，因为其原生权限匹配的是搜索表达式。参考：[OpenCode permissions](https://opencode.ai/docs/permissions/)。

部署时将总表放到 common/frontends.json，将每个 frontend 的原生配置、权限及凭据文件放进 common/<id>/config/，业务内容放进 common/<id>/app/。CU 挂载 common 根用于读取总表；桶分别只读挂载自己的 app、config 子目录和总表文件。服务加载 config，用户文件工具拒绝访问整个 config 目录，Bash 沙箱完全不挂载 config；新增配置文件也不会暴露。MinIO 配置在对应 frontend 的 attachment_storage 中，可直接填写凭据或引用本 frontend 的 config 内的凭据文件。配置字段参考 https://opencode.ai/docs/config/ 。

用户发送独立的 `/cancel`（允许首尾空白，忽略大小写）时，runtime 在排队前处理，只取消相同 frontend/user/conversation 的活动及排队请求。活动会话调用 `POST /session/{sessionID}/abort`，停止沙箱执行；取消消息本身返回确认，不提交给模型。重放同一个 request_id 不重复停止，无活动请求时返回提示。接口参考 https://opencode.ai/docs/server/ 。

先检查 schema：

```bash
python -m container_up --check-config --config config/opencode/frontends.json
```

schema 检查不代表凭据存在、模型可用或 bwrap 已通过启动测试。

连接公网的 Linux x64 构建机：

```bash
bash scripts/vendor_opencode_backend.sh
docker build -f Dockerfile.container_up -t agent-container-up:opencode-1.18.31 .
docker build -f Dockerfile.bucket_runtime -t agent-bucket-runtime:opencode-1.18.31 .
docker save agent-container-up:opencode-1.18.31 agent-bucket-runtime:opencode-1.18.31 -o agent-images.tar
```

APT、Chromium 和基础镜像获取发生在构建期；离线环境导入完成的镜像。不是在无基础依赖的机器上直接执行联网 Docker 构建。Python 包由带 hash 的 lock 文件和 wheelhouse 安装，插件由 npm lock 构建并包含离线依赖，runtime 不主动安装依赖；管理员额外开启的 OpenCode 自动更新、插件或 LSP 下载功能可能自行访问网络，离线部署需预置所需资源。

## 动态 bucket 池

CU 通过数据库事务预占名额、复用未满 bucket、按需创建 Deployment，空闲后 scale 到 0。一个 bucket 只有一个 runtime 和一个 OpenCode server，多个用户各自使用 session。

1. 全新部署只有 CU（以及 Service/RBAC/PVC 等资源），没有 bucket Pod。
2. CU 在 SQLite 的 bucket/binding 记录中原子预占用户容量。有未完成请求的用户继续使用当前桶，避免同时操作其工作区；其余用户只按本 frontend 活跃桶容量分配，没有容量则创建 bucket，不参考历史会话所在桶。
3. max_users_per_bucket 限制正在分配或有排队/执行中请求的不同用户数；用户全部请求完成即释放名额。runtime.limits.active 另行限制同时执行的请求数，两者不是同一个限制。
4. CU 调用 Kubernetes API 创建 Deployment，runtime 启动唯一的 OpenCode server、完成插件初始化并连接 CU 的 WS 后才派发请求。启动失败释放用户预占。换桶时，CU 在用户锁内将原生历史读入内存、删除旧桶副本、导入新桶、更新会话位置，再提交新消息。
5. 所有任务完成后开始 bucket 闲置计时。instance_idle_ttl_seconds 达到后整体 scale 到 0，等待 Pod 退出，保留尚未迁移的会话数据。0 禁用自动回收。存在排队/执行中请求、启动/迁移预占或未 ACK 命令时不回收。回收失败保留 draining 状态并重试。
6. Deployment 对象及服务器数据保留，Pod 被销毁；停止的桶不再参与新用户分配，其会话可从持久卷迁出。CU 重启从数据库恢复会话位置和未完成请求，未完成请求仍恢复到原 bucket。

K8s 示例：k8s/opencode/backend.yaml。CU 的 ServiceAccount 只授予当前命名空间内 Deployment 创建/查询/更新、scale 查询/更新和 Pod 查询。runtime 不挂载 ServiceAccount token。CU/runtime WS 不使用通信认证密钥；CU 仍检查 bucket ID 已登记且处于启动/运行状态，并校验请求归属。

frontends.json 的 control.bucket_pool 配置镜像、命名空间、max_users_per_bucket、启动超时、扫描间隔、PVC 名称与节点选择器。两种服务挂载同一份 frontends.json，不生成独立 runtime 配置。配置更改需要重启 CU，并在 bucket 重建时生效。

服务器存储由 k8s/opencode/storage.yaml 提供：data PVC 在 CU 挂载 /mnt/env，在 bucket 仅挂载 workspaces/<frontend_id> 子目录到 /mnt/env/workspaces/<frontend_id>；source PVC → /mnt/source（只读），common PVC 在 CU 挂载 /mnt/common，在 bucket 分别挂载 /mnt/common/<frontend_id>/app、/mnt/common/<frontend_id>/config 和单独的 frontends.json（只读）。物理目录统一为 /data/opencode_backend_k8s/{data,source,common}，默认用户工作目录为 /mnt/env/workspaces/<frontend>/<user>。storage.yaml 通过 storage_backend 选择 local 或 NFS；服务 SQLite/OpenCode 状态另用本地 state PVC → /data。每次收到请求和真正开始执行前均检查工作目录，缺失时重建，并补齐 output/scratch；不再复制模板；在各 frontend 的 opencode-server.json 中用 instructions 指定容器内绝对路径 /mnt/common/<frontend_id>/app/templates/AGENTS.md。OpenCode server 在 context_root 启动，但每个会话的创建、消息、取消等请求都显式使用该用户工作目录作为 directory，按用户与 chat_id 创建或复用 session；共享规则从 instructions 指定文件加载；用户目录已有的 AGENTS.md 仍由 OpenCode 额外加载。本 frontend 的 app 自动加入用户权限和 bwrap 只读挂载，无需配置 public_roots。

复用会话时核对 OpenCode 返回的 directory，不匹配则返回 SESSION_WORKSPACE_MISMATCH，不切换目录或自动创建替代会话。

完整部署步骤、节点权限、配置文件、NodePort 与 local/NFS 切换见 [Kubernetes 部署说明](../k8s/opencode/README.md)。

用户重新请求时使用原 bucket、用户目录和 OpenCode session。CU 不维护历史重建协议，不把历史正文拼接到新会话。

部署前创建以下资源（命名空间须与 bucket_pool.namespace 一致）：

```bash
kubectl apply -f k8s/opencode/storage.yaml
cp config/opencode/frontends.json /data/opencode_backend_k8s/common/frontends.json
# 在存储节点准备文件，确保 UID/GID 10001 可读：
# /data/opencode_backend_k8s/common/<frontend_id>/config/opencode-server.json
# /data/opencode_backend_k8s/common/<frontend_id>/config/user-permissions.yaml
# 无需创建 ConfigMap 或 Secret，详细准备命令见 k8s/opencode/README.md。
kubectl apply -f k8s/opencode/backend.yaml
```

现有 bwrap 节点要求不变：允许非特权 user namespace，并配置兼容的 seccomp/AppArmor 策略。附件仅传引用；publish_attachment 继续在 bucket 内上传 MinIO，CU 不下载文件。

## 渠道与业务服务

- QXT 支持 group_chat_at_bot，保留群 ID 为可信回复目标。QXT/飞书统一发送对象 URL，渠道不下载或重新上传附件。详见 ATTACHMENT_UPLOAD_API.md。
- 新增 hazard_expert_mcp，业务客户端可上传 Base64 附件到 MinIO，再调用分析工具请求 CU /inbound-sync。部署已改用 opencode-sv、opencode-source-pvc、opencode-runtime=true 和 CU 8000 端口。它不是应注册给同一 OpenCode 的模型工具，否则可能递归请求自身。
- 订阅失败记录 frontend/chat 上下文并传播错误；取消不被吞掉。Supervisor 使用继承的 stdout/stderr，runtime serve 在 finally 停止 OpenCode 子进程。
- Web 代理要求显式 frontend_id、user_id、chat_id，额外字段直接拒绝；代理不传递 Authorization。
- 本地文件由 runtime publish_attachment 发布；附件引用接口支持 JSON 引用和 QXT outbound URL。业务客户端可通过 hazard_expert_mcp 上传 Base64 文件到 MinIO。

代码主链路齐备，但并非零配置生产部署：示例模型、渠道、MinIO、密钥必须替换；飞书私有附件没有自动授权下载器，图片引用不会自动转为模型视觉输入；CU/bucket WS 仅校验登记 ID、没有通信认证，部署清单未提供 NetworkPolicy；共享 OpenCode 的原生/MCP I/O 也不等同于每用户 OS 隔离。生产部署前仍需验证真实渠道、模型、对象存储与集群节点能力。

## 会话隔离边界与源码更新

每个桶共用一个 OpenCode 服务和服务级认证，用户会话按 frontend/user/chat 映射，正常请求不混用历史。工作区文件通过 hook/session 规则和 bwrap 限制，但 OpenCode 主进程、MCP 等可信扩展不是按用户隔离的进程。

CU 接收调用方提供的 frontend_id、user_id，不再提供入站 API key 校验。目录范围仍按这两个身份字段确定，query/cancel 校验 frontend 归属；调用业务端负责用户身份与请求归属。

source 在每个服务中统一只读挂载为 /mnt/source；CU/runtime/Web 在该目录启动，MCP 在其对应项目子目录启动。插件位于 /mnt/source/opencode_plugin；业务脚本与 skills 位于 /mnt/common/<frontend_id>/app。所有桶的二进制来自同一服务器目录 /data/opencode_backend_k8s/opencode，文件 /opt/opencode/opencode 只读。更新源码、重新构建插件（npm ci && npm run build）或替换二进制均不需要重建镜像；基础依赖变化仍需更新运行环境。见 Kubernetes 部署说明中的部署更新步骤。

## 按 frontend 隔离业务公共资料

默认业务公共目录为 `/mnt/common/{frontend_id}/app`，由当前请求身份展开。运行配置由 frontends.json 注入绑定 ID，错误 frontend 的请求在建工作区/登记任务前被拒绝。本 frontend 的 app 始终整棵共享给所属 bucket；用户权限仅控制额外公共目录，不能扩展到 config、frontend 根、common 根目录、父目录或其他 frontend。各 frontend 的资料、脚本和 skills 统一放在自己的 app 下。

OpenCode skills.paths 中的 `/mnt/common/{frontend_id}/app/skills` 在启动时展开，越界路径拒绝启动，避免全局技能扫描读到其他 frontend 的说明。动态 bucket 分开挂载本 frontend 的 app 和 config，不挂载 frontend 根或整个 common；服务配置 frontends.json 使用独立的只读文件挂载。原生文件工具、session 路径规则和 bwrap 使用同一公共范围。相关部署准备与挂载说明见 Kubernetes 部署说明。

消息发送、最终渠道投递和同步接口等待由事件唤醒；心跳及 bucket 闲置检查使用定时器。插件在持久 state 目录保存全部未确认事件，runtime 接收成功后才删除；只有 runtime 判定最终回复。调用方必须提供非空 chat_id。
