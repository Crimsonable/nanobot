# Kubernetes 部署

首次部署、离线镜像导入、同步/异步消息、取消及附件测试的完整命令见 [部署与测试手册](../../docs/DEPLOYMENT_MANUAL.md)。

会话过程日志配置与 ID 字段升级说明见 [日志与 ID 约定](../../docs/SESSION_LOGGING.md)。日志由插件写入 bucket 的 runtime 容器 stdout。

一个 container-up（CU）读取 `common/frontends.json` 总表，接入多个 frontend。每个 frontend 有自己的 OpenCode 配置、用户权限、skills、模板和工作空间；动态桶固定属于一个 frontend，不同 frontend 不共用一个 OpenCode 进程。初始只启动 CU，收到请求后创建桶，不部署固定 StatefulSet。

CU 入站、查询、取消、附件引用接口和 Web 代理不需要 API key。未知 frontend 返回 404；bucket 登记、请求归属、运行时内部插件调用凭据继续保留。渠道自己的签名/凭据、模型服务凭据和 MinIO 凭据按各自服务要求配置。

## 目录与配置作用域

服务器目录默认如下。source 和 OpenCode 二进制共用，业务配置与数据按 frontend 分区。

```text
/data/opencode_backend_k8s/
├── source/                         # 服务源码、opencode_plugin 及其构建产物和依赖
├── opencode/opencode               # 全部桶共用的一份程序
├── common/
│   ├── frontends.json              # 入口总表；一个 CU 接入全部注册项
│   ├── web-test/
│   │   ├── config/                # 服务配置，不对用户工具开放
│   │   │   ├── opencode-server.json  # 仅 web-test 的模型、provider、MCP 等
│   │   │   └── user-permissions.yaml # defaults 仅作用于 web-test 的用户
│   │   └── app/                   # 本 frontend 公共只读内容
│   │       ├── docs/
│   │       ├── skills/
│   │       └── templates/
│   └── qxt-main/                   # 同样有独立配置、skills、模板
├── data/workspaces/
│   ├── web-test/alice/
│   └── qxt-main/alice/              # 相同 user_id 的另一份工作空间
└── state/
    ├── control/                    # CU 数据库
    └── runtime/<frontend_id>/<bucket_id>/  # runtime/OpenCode 状态
```

配置示例在 `config/opencode/frontends.json` 和 `config/opencode/web-test/config/`。总表顶层 `control` 是 CU/集群基础设施参数；`frontends` 数组中的每一项配置一个 frontend：

| 配置位置 | 作用范围与内容 |
| --- | --- |
| control | CU 监听、状态根、bucket_pool 镜像、namespace、PVC、节点、单桶容量 |
| frontends[].id / provider / channel | 该 frontend 的 ID、渠道、渠道凭据与参数 |
| frontends[].send_msg_url / attachment_storage | 该 frontend 的回调及附件存储 |
| common/<id>/config/opencode-server.json 的 model / instructions | 该 frontend 使用的模型与规则文件 |
| frontends[].instance_idle_ttl_seconds | 该 frontend 桶的闲置回收时间，0 禁用回收 |
| common/<id>/config/opencode-server.json | 该 frontend 的 provider、模型连接/API key、MCP、LSP、skills 等原生配置 |
| common/<id>/config/user-permissions.yaml | 该 frontend 的默认用户权限及逐用户覆盖 |

总表示例：

```json
{
  "control": {"bucket_pool": {"image": "agent-bucket-runtime:opencode-1.18.31-bwrap011", "max_users_per_bucket": 16}},
  "frontends": [
    {"id": "web-test", "provider": "web", "runtime": {}},
    {"id": "qxt-main", "provider": "qxt", "channel": {"app_id": "REPLACE", "app_secret": "REPLACE"},
     "runtime": {}}
  ]
}
```

两个 frontend 可以使用相同的 provider/model 名称，读取的原生配置文件不同。模型服务凭据填写在各自 `config/opencode-server.json` 的 provider.options.apiKey 中；MinIO 使用凭据文件时也必须放在该 frontend 的 config 内。配置路径不能改到 app，模板路径不能改到 config，符号链接别名被拒绝。

用户文件工具与 skills 只能访问当前 frontend 的 app 子树，Bash 沙箱完全不挂载 config 目录；新增配置文件也不会暴露。可以递归搜索整个 app，但不能读取或搜索 frontend 根目录、config 或其他 frontend。用户自己的工作空间继续按权限开放。frontend 桶要求 sandbox.enabled 和 session.enabled 均为 true，关闭任一项会拒绝启动。服务进程仍需加载 config；这一边界约束模型的文件工具和 Bash，管理员配置的原生插件/MCP 应视作受信服务代码。

权限文件中的 `defaults` 只影响当前 frontend，`users` 可只填写 user_id；若显式提供 frontend_id，必须与当前目录的 frontend 一致。完整开关见 [实现说明](../../docs/OPENCODE_BACKEND_IMPLEMENTATION.md)。

## 默认路径、端口与资源

| 配置 | 默认值 |
| --- | --- |
| CU / 命名空间 | container-up:8000 / opencode-sv |
| CU NodePort | 30080 |
| bucket_pool.name_prefix / max_users_per_bucket | opencode-bucket / 16 |
| bucket_pool.node_selector | opencode-runtime=true |
| 启动等待 / 扫描间隔 | 120 秒 / 10 秒 |
| runtime.control_url | ws://container-up:8000/internal/buckets/ws |
| runtime.workspace_root | /mnt/env/workspaces |
| OpenCode / 插件 RPC / 健康端口 | 4096 / 4097 / 8090 |
| OpenCode 二进制 / 插件 | /opt/opencode/opencode / /mnt/source/opencode_plugin/dist/index.mjs |
| bucket_pool.opencode_host_path | /data/opencode_backend_k8s/opencode |
| 配置 / 用户权限 | /mnt/common/<id>/config/opencode-server.json / /mnt/common/<id>/config/user-permissions.yaml |
| 模板 / skills | /mnt/common/<id>/app/templates / /mnt/common/<id>/app/skills |
| 公共目录 | /mnt/common/<id>/app（只读） |
| 单桶 CPU request / limit | 1 / 4 |
| 单桶内存 request / limit | 1Gi / 4Gi |
| 执行并发 / 额外排队容量 | 4 / 64 |
| 请求期限 / 单附件上限 | 600 秒 / 50 MiB |

OpenCode 与插件 RPC 只监听桶内回环地址，不需要为每桶创建 Service 或 NodePort。CU 的 ServiceAccount/RBAC 用于创建和缩容 Deployment，Kubernetes 自动挂载的 API token 保留；桶不挂载 ServiceAccount token。

## 存储、挂载和权限

| 服务器目录 | PVC | 挂载 |
| --- | --- | --- |
| data | opencode-data-pvc | CU: /mnt/env；桶仅挂载 workspaces/<id> 到 /mnt/env/workspaces/<id>，读写 |
| source | opencode-source-pvc | /mnt/source，只读 |
| common | opencode-common-pvc | CU: /mnt/common；桶分别挂载 <id>/app、<id>/config 及单独的 frontends.json，只读 |
| state | opencode-state-pvc | /data，读写，本地盘 |

`storage.yaml` 默认 `&storage_backend local`。改用 NFS 时，须在首次 PVC 绑定前选 nfs 并修改三个 NFS PV 的 server/export；state 仍用本地盘，避免 SQLite WAL 放到 NFS。已绑定 PVC 不会因为改 selector 自动迁移，所有 PV 回收策略为 Retain。

选择一个存储节点，只给它设置 `opencode-runtime=true` 标签。UID/GID 10001 必须能读取 source/common、执行共享二进制、写入 state 和用户目录。所有 hostPath/subPath 目录都要预先准备，已有用户目录也要检查权限。

每个 frontend ID 必须是单个有效目录名，禁止路径分隔符、点开头以及保留名 frontend.json/frontends.json，不进行有损清洗。用户目录默认为 workspaces/<frontend>/<user>，user 名称清洗导致碰撞时 CU 拒绝共用目录。

runtime 每次收到请求以及执行前检查用户目录，缺失时重建并补齐 output/scratch。不再复制 templates；各 frontend 的 config/opencode-server.json 用 instructions 引用 /mnt/common/<id>/app/templates/AGENTS.md（容器内挂载路径，不能填写服务器 /data/opencode_backend_k8s/common 路径）。一个 bucket 只运行一个 OpenCode server；每个会话的 API 请求 directory 指向对应用户工作目录，相同用户和 chat_id 复用会话，不同 chat_id 使用不同会话但共用该用户目录。context_root 仅用于服务启动和初始化。原生文件工具与 Bash 进一步限定到当前用户目录；其他 frontend 的资料和工作区不会挂入当前桶。

## 首次部署

在联网 Linux x86_64 构建机的项目根目录准备 Python 3.12、npm、curl、Docker：

```bash
bash scripts/vendor_opencode_backend.sh
docker build -f Dockerfile.container_up -t agent-container-up:opencode-1.18.31 .
docker build -f Dockerfile.bucket_runtime -t agent-bucket-runtime:opencode-1.18.31-bwrap011 .
```

脚本下载校验 OpenCode 和固定的 Debian bubblewrap 0.11.0-2+deb13u1 包、准备 Python wheels，并构建插件。已有镜像的旧内核修复及导入命令见[部署手册第 2.1 节](../../docs/DEPLOYMENT_MANUAL.md#21-已有镜像的-bwrap-旧版修复419-节点)。镜像推入内网仓库或导入节点实际使用的运行时；正式更新应使用新镜像标签，并同步修改 backend.yaml 与总表 control.bucket_pool.image。

在存储节点创建目录并复制当前源码与构建结果。下面仅用于首次初始化，不覆盖已有配置：

```bash
DEPLOY_ROOT=/data/opencode_backend_k8s
mkdir -p "$DEPLOY_ROOT"/{data,source,common,state,opencode}
mkdir -p "$DEPLOY_ROOT/common/web-test/config"
mkdir -p "$DEPLOY_ROOT/common/web-test/app"/{docs,skills,templates}
mkdir -p "$DEPLOY_ROOT/data/workspaces/web-test"
rsync -a --exclude=.git/ --exclude=.venv/ --exclude=.opencode-reference/ --exclude=vendor/ ./ "$DEPLOY_ROOT/source/"
cp config/opencode/frontends.json "$DEPLOY_ROOT/common/frontends.json"
cp config/opencode/web-test/config/opencode-server.json "$DEPLOY_ROOT/common/web-test/config/"
cp config/opencode/web-test/config/user-permissions.yaml "$DEPLOY_ROOT/common/web-test/config/"
cp config/opencode/web-test/config/session-logging.json "$DEPLOY_ROOT/common/web-test/config/"
install -m 0555 vendor/opencode "$DEPLOY_ROOT/opencode/opencode.new"
mv -f "$DEPLOY_ROOT/opencode/opencode.new" "$DEPLOY_ROOT/opencode/opencode"
```

新增 frontend 时，在总表添加一项并准备其 common/<id>/config、common/<id>/app 和 data/workspaces/<id> 目录；无需再部署一个 CU。两个 common 子目录都必须在建桶前存在，Kubernetes subPath 不负责初始化。把模型、回调、渠道和 MinIO 占位值替换为实际值。各自原生配置中的 model 使用 provider/model 格式，runtime 不覆盖该选择。

MinIO bucket 需提前创建并授予必要权限。public_base_url 非空时返回拼接的未签名链接，须保证可下载；为空时生成基于 endpoint 的预签名链接，endpoint 也须对下载方可达。需要在 Bash 下载附件时，按该 frontend 的需求设置 sandbox.network；默认 true，Bash 沙箱共享 bucket 的网络；模型请求和 runtime 上传 MinIO 不经过 Bash 网络开关。

检查配置并部署：

```bash
python -m container_up --check-config --config config/opencode/frontends.json
kubectl label node 实际节点名 opencode-runtime=true
kubectl apply -f k8s/opencode/storage.yaml
kubectl apply -f k8s/opencode/backend.yaml
kubectl -n opencode-sv get pvc,pods,deployments
kubectl -n opencode-sv logs deployment/container-up --tail=100
```

schema 检查不验证模型连通性、文件权限或 bwrap 能力。节点需允许非特权 user namespace 和 bwrap 所需的 seccomp/AppArmor 操作，runtime 会实际探测且不会静默关闭隔离。初始没有桶是正常情况。使用无 Authorization 头的 `/inbound-sync/<id>` 发送测试请求，验证建桶、模型响应、文件生成和附件下载。CU 健康检查通过不代表这些链路已完成验证。

## 源码与二进制更新

所有服务源码统一挂载到 /mnt/source：CU/runtime/Web 在该目录启动，MCP 在对应项目子目录启动。保留 opencode_plugin/dist/index.mjs、package.json、package-lock.json、node_modules；插件修改后重新构建。各 frontend 的资料、脚本、skills、模板和配置均放在 common/<id>；部署时将仓库 config/opencode/<id>/app 与 config 分别同步到该 frontend 的 app 与 config。

镜像只保留 Python、基础依赖和系统工具。同步源码或插件不需要重建镜像；依赖变化才更新镜像。二进制放在独立 opencode 目录，先写新文件再同目录 rename，避免覆盖运行中的程序；示例原生配置 autoupdate=false。

在无执行中任务时同步文件并重启受影响服务。修改单个 frontend 的 OpenCode/权限配置只需重启属于它的桶；修改入口总表需重启 CU 和相关桶。Deployment 的 `opencode.io/frontend-id` annotation 标识归属；session 权限变化时应先完成该 frontend 的整桶回收再更新配置，下一次分配创建新 session；仅重启并复用旧 session 会因权限回读不匹配而拒绝执行。

CU 的启动命令是 `python -m container_up --config /mnt/common/frontends.json`；CU 创建的 bucket 使用 `python -m bucket_runtime` 并传入配置、bucket ID 和 frontend ID。`agent_backend` 仅为公共库，不提供启动器。升级旧统一启动入口时，需重新应用 `k8s/opencode/backend.yaml`，并使用下文的 `scripts.render_bucket_updates` 更新已有 bucket 的 Deployment 启动参数；仅 rollout restart 不会更新旧命令。直接使用镜像默认 CMD 的部署需要采用更新后的镜像或显式指定新启动命令。

```bash
kubectl -n opencode-sv rollout restart deployment/container-up
# 全部桶需要更新时：
kubectl -n opencode-sv rollout restart deployment -l app=opencode-bucket
```

## 更新 bucket Deployment

用户全部请求结束后释放 bucket 容量，再次请求按本 frontend 活跃桶容量分配。CU 在 control.sqlite 持久化 frontend/user/chat 到 session_id、bucket_id 的会话位置；换桶时经内存迁移原生历史并删除旧桶会话副本，已停止旧桶直接从 state PVC 恢复，无需唤醒。未访问的历史仍可能保存在旧桶目录中，不要删除状态目录来更新源码。迁移过程不落中间文件，接受删除源历史后 CU 崩溃导致历史丢失的窗口，详见 [日志与会话说明](../../docs/SESSION_LOGGING.md)。

```bash
python -m scripts.render_bucket_updates --config /data/opencode_backend_k8s/common/frontends.json > /tmp/opencode-bucket-updates.yaml
kubectl apply -f /tmp/opencode-bucket-updates.yaml
```

更新工具要求每个 bucket 有明确的 frontend annotation，保留当前副本数，不推断归属。启动时不执行旧数据或配置迁移。

## 可选服务

Web 代理清单为 k8s/opencode/web-server.yaml；不检查或透传 CU API key。作为异步回调接收器时，把对应 frontend 的 send_msg_url 设为 http://web-server:8000/outbound；同步请求直接访问 CU /inbound-sync/<id>。

工程场景 MCP 使用 k8s/opencode/engineering-scene-mcp.yaml（8090 / NodePort 30087）；隐患专家 MCP 使用 k8s/opencode/hazard-expert-mcp.yaml（8082 / NodePort 30088）。源码均挂载统一 source 卷，分别构建其基础镜像。隐患专家配置 MCP_API_KEY、MinIO 及已注册的 CONTAINER_UP_FRONTEND_ID，调用 CU 不需要额外凭据。它是业务入口，不要注册为同一 CU 的模型工具以免递归调用，详见 ../../hazard_expert_mcp/README.md。

该 frontend 的 app 整棵目录始终作为只读公共目录提供给其所有 bucket 和用户，无需单独放行 templates。filesystem.public_roots 仅控制额外公共目录，即使设为 [] 也不会移除本 frontend 的 app；config 和其他 frontend 的目录仍不可访问。
