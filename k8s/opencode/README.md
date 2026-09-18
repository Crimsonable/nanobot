# Kubernetes 部署

初始只运行 container-up。请求到达后，CU 根据 frontend.json 创建 bucket Deployment，每个 bucket 内一个 runtime 和一个 OpenCode server。不要额外部署固定 bucket StatefulSet。

frontend.json 直接放在服务器 `/data/nanobot_backend_k8s/common/frontend.json`，通过 common PVC 整目录只读挂载。CU 和 runtime 均读取 `/mnt/common/frontend.json`，无需创建或更新配置 Secret。NFS 模式下修改对应 export 中的文件。

修改后重启 CU 加载配置；runtime 配置修改还需重启相关 bucket，没有自动热加载。已运行 Deployment 的镜像更新仍需重新创建/唤醒或修改 Deployment 镜像。其他两个配置文件仍使用 agent-config ConfigMap，模型和前端 API 凭据仍使用各自的 Secret。

默认开启权限控制时，frontend.json 被视为服务配置：原生 read 无法读取，grep/LSP 不允许对包含它的目录做递归访问（可以搜索 common 下的资料子目录）；bwrap 屏蔽该文件的真实内容。服务进程仍可读取真实配置。显式关闭 session/hook 或 bwrap 隔离时，相应保护也会关闭。

## 精简配置与代码默认值

frontend.json 只列出渠道/回调、MinIO、整桶闲置时间、bucket 镜像与用户容量、模型及指令。以下字段无需重复填写；需要定制时仍可在原位置覆盖。

| 配置 | 默认值 |
| --- | --- |
| profile_id / api_key_file | reports / /run/secrets/web-api-key |
| control.host / port / state_root | 0.0.0.0 / 8000 / /data/control |
| bucket_pool.namespace / name_prefix | opencode-sv / opencode-bucket |
| 启动等待 / 扫描间隔 | 120 秒 / 10 秒 |
| PVC | opencode-data-pvc、opencode-source-pvc、opencode-common-pvc、opencode-state-pvc |
| node_selector | opencode-runtime=true |
| ConfigMap / runtime Secret | agent-config / agent-runtime-secrets |
| runtime.control_url | ws://container-up:8000/internal/buckets/ws |
| runtime.state_root / workspace_root | /data/runtime / /mnt/env/workspaces |
| 工作目录布局 / 模板 | frontend_user / /mnt/source/templates |
| OpenCode / 插件 RPC / 健康端口 | 4096 / 4097 / 8090 |
| OpenCode 二进制 / 插件 | /opt/opencode/opencode / /opt/opencode-plugin/dist/index.mjs |
| OpenCode 配置 / 用户权限 | /etc/agent/opencode-server.json / /etc/agent/user-permissions.yaml |
| 默认公共目录 | /mnt/common、/opt/public |
| 执行并发 / 额外排队容量 | 4 / 64 |
| 请求期限 / 单附件上限 | 600 秒 / 50 MiB |

control 整段省略时仍默认开启动态 bucket 池。CU/runtime WS 已取消通信密钥与 Authorization 请求头，只按已登记 bucket ID 识别连接；请求归属检查仍保留。前端入站认证、MinIO/模型凭据以及进程内部自动生成的插件调用凭据不变。

## 存储与旧部署的对应关系

沿用旧 k8s/base/base.yaml 的 PV/PVC、local/NFS 选择和服务器物理目录，资源名称改为 opencode。已有工作文件无需搬迁。

| 服务器目录（local 或 NFS export） | PVC | CU/bucket 容器路径 | 用途 |
| --- | --- | --- | --- |
| /data/nanobot_backend_k8s/data | opencode-data-pvc | /mnt/env，读写 | 原用户工作目录 |
| /data/nanobot_backend_k8s/source | opencode-source-pvc | /mnt/source，只读 | 可选工作区初始化模板 |
| /data/nanobot_backend_k8s/common | opencode-common-pvc | /mnt/common，只读 | 所有默认用户可见的公共资料、skills |
| /data/opencode_backend_k8s/state | opencode-state-pvc | /data，读写 | CU 数据库、各 bucket 的 runtime/OpenCode 状态 |

保留物理路径中的旧名称是为了直接使用现有数据，不再从 source 卷启动旧服务代码。服务代码、二进制和基础技能随镜像提供，镜像内 /opt/public 仍可读。

用户目录为 `/mnt/env/workspaces/<frontend_id>/<user_id>`，沿用原来的标识符清洗规则。runtime 对每个收到的请求检查目录，任务真正开始执行前再次检查；目录缺失时创建，补齐 output/scratch。新建目录时，若 `/mnt/source/templates` 存在则复制模板；不覆盖已有用户文件。不把普通文件或符号链接当作合法目录，不删除它们，而是返回 WORKSPACE_UNAVAILABLE。

`/mnt/common` 同时列入 frontend.json 的 runtime.profiles.*.public_roots 和 OpenCode skills 路径。默认用户权限继承公共目录；bwrap 将公共目录只读挂载、仅将当前用户工作目录按权限挂载。管理员仍可在 user-permissions.yaml 中缩小某用户的目录范围。

## 部署步骤

1. 准备上述服务器目录。UID/GID 10001 必须能写数据、状态目录及原有用户目录，能读 source/common；已有文件不要覆盖。PV 使用 Directory 类型，目录缺失会阻止挂载。选择一个存储节点，执行 `kubectl label node <节点名> opencode-runtime=true`；只为该节点设置此标签。
2. 编辑 storage.yaml。默认 `&storage_backend local`，使用节点上的原目录。需要 NFS 时在首次绑定前改为 `nfs`，并修改三个 NFS PV 的 server/export path。已绑定 PVC 不会因修改 selector 自动迁移，切换已有数据卷应单独安排。state PVC 始终使用本地盘，避免 SQLite WAL 数据库放到 NFS 上；PV 回收策略均为 Retain。
3. 编辑服务器 common/frontend.json（首次从 config/opencode/frontend.json 复制）：模型 profile、回调、MinIO、bucket 容量和 TTL；control.bucket_pool 中的 PVC 名称、namespace、node_selector 必须与部署文件一致。默认命名空间 opencode-sv。配置 OpenCode 原生 JSON 和用户权限文件。
4. 创建存储资源、配置与凭据，最后启动 CU：

```bash
kubectl apply -f k8s/opencode/storage.yaml

cp config/opencode/frontend.json /data/nanobot_backend_k8s/common/frontend.json
kubectl -n opencode-sv create configmap agent-config \
  --from-file=config/opencode/opencode-server.json \
  --from-file=config/opencode/user-permissions.yaml

# 本地准备前端 API 密钥和模型 API 密钥。
kubectl -n opencode-sv create secret generic agent-control-secrets \
  --from-file=web-api-key=/secure/web-api-key
kubectl -n opencode-sv create secret generic agent-runtime-secrets \
  --from-file=opencode-api-key=/secure/opencode-api-key

kubectl apply -f k8s/opencode/backend.yaml
kubectl -n opencode-sv get pvc,pods,deployments
```

web-api-key 至少 16 字符。CU/runtime WS 不再需要通信密钥。MinIO 直接读取 frontend.json 的 attachment_storage，无需另一份 runtime 配置。镜像须预先导入节点或推送到内网仓库；同时修改 backend.yaml 的 CU 镜像和 frontend.json 的 bucket_pool.image。

默认 CU NodePort 为 30080（容器端口 8000）。若旧服务仍占用 30080，修改新 Service 的 nodePort 后部署。CU 与 bucket 通过集群内部 Service 地址建立 WS，不需要为每个 bucket 暴露 Service 或 NodePort。

节点需支持非特权 user namespace，并允许 bwrap 所需的 seccomp/AppArmor 操作。运行时有 bwrap 启动探测，不会静默跳过隔离。

## 生命周期

`max_users_per_bucket` 控制 bucket 存活期间分配的不同用户数；没有每用户进程或每用户回收。所有请求结束且闲置达到 `instance_idle_ttl_seconds` 后整桶缩容至零，并释放映射。0 禁用自动回收。Pod 销毁不会删除 PVC、用户文件或服务数据库；下一次请求可以重新分配到其他 bucket。
