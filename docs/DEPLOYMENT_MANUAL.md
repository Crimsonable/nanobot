# OpenCode backend 部署与运维手册

Channel Gateway 与 CU 共用一个单副本 Pod，分别运行在 `gateway` 和 `control` 容器。外部 NodePort 30081 指向 Gateway，CU 仅供内部控制连接；分层升级及已有独立 Deployment 的合并步骤见 [渠道分层部署说明](CHANNEL_GATEWAY.md)。


本文按首次部署的操作顺序编排。命令适用于 **Linux x86_64 + Bash**，不适用于 Windows PowerShell。以现有 Kubernetes 集群、一个运行节点、`web-test` frontend 为例；本地盘和 NFS 二选一。旧 nanobot 的配置和数据库不能直接复用。

首次部署依次执行第 1～6 章，然后跳到第 11 章验收。第 7～10 章是日常操作，**不要在首次部署时顺着执行停服、卸载命令**。

| 章节 | 要完成的事 |
| --- | --- |
| [1. 环境与部署参数](#1-环境与部署参数) | 确认机器分工、依赖和网络 |
| [2. 节点配置](#2-节点配置) | 选择唯一运行节点，核对调度条件 |
| [3. 目录与文件位置](#3-目录与文件位置) | 明确源码、配置、数据、二进制分别放哪里 |
| [4. 存储配置](#4-存储配置本地盘或-nfs) | 选择本地盘，或配置 NFS 服务端及客户端 |
| [5. 构建镜像与放置文件](#5-构建镜像与放置文件) | 打包、传输、导入镜像、填写业务配置 |
| [6. 启动服务](#6-启动服务) | 创建 PVC、启动 CU、检查就绪状态 |
| [7. 关闭与恢复服务](#7-关闭与恢复服务) | 保留数据停服，再恢复入口 |
| [8. 更新服务](#8-更新服务) | 更新源码、配置、镜像和已有 bucket |
| [9. 删除命名空间与重装](#9-删除命名空间与重装) | 卸载资源、保留磁盘数据 |
| [10. 日志与排障](#10-日志与排障) | 查看 CU、bucket、上一次崩溃日志及事件 |
| [11. 测试命令](#11-测试命令) | 同步、会话、异步、取消、附件与开发回归 |

## 1. 环境与部署参数

### 1.1 服务怎样运行

`container-up` Deployment 固定运行一个副本，包含 Gateway 与 CU 两个容器。Gateway 是统一业务入口，CU 负责调度；两者读取 `common/frontends.json`，收到用户请求后由 CU 动态创建 bucket Deployment。每个 bucket 运行 runtime 和 OpenCode，仅服务一个 frontend。首次启动只有 Gateway/CU Pod、没有 bucket 是正常现象；无需手工启动 runtime，也没有固定 StatefulSet。

可选 `web-server` 用于 Web 代理和异步回调测试。模型服务和 MinIO 需提前可用：模型供 OpenCode 调用，MinIO 用于附件发布。当前 Gateway 入站、查询、取消接口不要求 Authorization 头；模型、渠道和 MinIO 各自仍需实际凭据。

### 1.2 哪台机器做什么

| 机器角色 | 必要条件 | 执行的步骤 |
| --- | --- | --- |
| 联网构建机 | Linux x86_64、Python 3.12/pip、Node.js/npm、curl、tar、Docker；可访问依赖源 | 下载依赖、构建插件与镜像、输出交付包 |
| kubectl 操作机 | kubectl、有效 kubeconfig、curl；能管理目标 namespace/PV/RBAC | 修改并应用清单、启停、日志、接口测试 |
| Kubernetes 运行节点 | Linux x86_64、可用的容器运行时、足够磁盘；sudo、tar、util-linux | 导入镜像，保存本地 state 和 OpenCode 程序 |
| 文件存储机 | 本地模式就是运行节点；NFS 模式就是 NFS 服务端 | 放置 source、common、data，填写配置 |

这些角色可以在同一台机器。远程操作时先登录相应机器，不要把构建机的路径误当成节点路径。集群安装本身不在本文范围。

当前仓库 CU 与 bucket 使用 **UID/GID 0 + privileged**；可选 Web 使用 UID/GID 10001。集群准入策略必须允许当前清单。不要再按旧文档假定所有进程都是 10001。privileged 权限较大，这是当前实现的部署条件；它也不能补齐旧内核缺失的系统调用。

节点须支持 runtime 的 bwrap 隔离。启动时会实际探测，失败不会自动关闭沙箱。单 bucket 请求 1 CPU / 1Gi 内存、上限 4 CPU / 4Gi；还需给 CU、系统和可选服务留余量。

### 1.3 固定参数与需替换的值

本文保留仓库默认 namespace、路径和镜像标签，避免只改一处造成不一致。

| 参数 | 本文使用的值 |
| --- | --- |
| namespace | `opencode-sv` |
| 部署根目录 | `/data/opencode_backend_k8s` |
| frontend | `web-test` |
| CU 容器 / Service 端口 | `8001` / `8000` |
| Gateway 容器 / Service 端口 | `8000` / `8000` |
| Gateway 调用 CU | `http://127.0.0.1:8001` |
| Gateway NodePort | **`30081`**，以 `backend.yaml` 为准 |
| 节点标签 | `opencode-runtime=true`，仅一个节点 |
| CU 镜像 | `agent-container-up:opencode-1.18.31` |
| bucket 镜像 | `agent-bucket-runtime:opencode-1.18.31-bwrap011` |

在 kubectl 操作机设置实际节点名和地址；以下 REPLACE 值必须替换，节点名用 `kubectl get nodes` 返回的名字：

```bash
kubectl get nodes -o wide
export NODE='REPLACE_WITH_NODE_NAME'
export NODE_IP='REPLACE_WITH_NODE_IP'
export GATEWAY_URL="http://${NODE_IP}:30081"
```

后文文件操作的终端均先设置：

```bash
export DEPLOY_ROOT=/data/opencode_backend_k8s
```

新开终端要重新设置变量。集群中应能解析 Service DNS；CU 能访问 Kubernetes API，bucket 能访问 CU、Gateway、模型服务、MinIO 与实际附件 URL。操作机须能访问节点 30081；OpenCode 4096 和插件 RPC 4097 仅在桶内回环监听，不需要对外开放。

## 2. 节点配置

**执行位置：kubectl 操作机。**先检查标签，再给选中的唯一运行节点加标签：

```bash
kubectl get nodes -L opencode-runtime
kubectl label node "$NODE" opencode-runtime=true --overwrite
kubectl describe node "$NODE"
kubectl get nodes -l opencode-runtime=true -o wide
```

最后一条应只返回一个 Ready 节点。若其他节点已有相同标签，先确认没有依赖该标签的在用负载，再用实际节点名移除：

```bash
kubectl label node REPLACE_WITH_OTHER_NODE opencode-runtime-
```

本项目 state PV 和 OpenCode hostPath 均在该节点，即使选择 NFS 也仍是单节点调度。**不能靠给多个节点打标签实现多节点部署。**检查节点没有阻碍调度的污点；若选择 control-plane 节点，需要按集群实际策略配置 tolerations，当前清单不自动容忍控制平面污点。

**执行位置：运行节点。**确认架构、内核和磁盘：

```bash
uname -m
uname -r
df -h /data
```

架构应为 `x86_64`；若 `/data` 尚不存在，先创建并确认其所在磁盘容量。旧 4.19 内核使用当前固定的 bwrap 镜像，相关说明见第 5.4 节。

## 3. 目录与文件位置

### 3.1 最终目录结构

```text
/data/opencode_backend_k8s/
├── source/                         # 仓库源码，不是仅一个 Python 包
│   ├── container_up/
│   ├── channel_gateway/
│   ├── bucket_runtime/
│   ├── agent_backend/
│   ├── web_server/
│   └── opencode_plugin/            # 必须含 dist、node_modules 和 package 文件
├── common/
│   ├── frontends.json              # 后端总表：渠道、镜像、调度、回调、MinIO
│   └── web-test/
│       ├── config/                 # 模型、权限、会话日志配置
│       │   ├── opencode-server.json
│       │   ├── user-permissions.yaml
│       │   └── session-logging.json
│       └── app/                    # 当前 frontend 的公共只读资料
│           ├── docs/
│           ├── skills/
│           └── templates/AGENTS.md
├── data/workspaces/web-test/       # 服务自动创建下级用户目录
├── state/                          # 必须在运行节点本地盘
│   ├── control/                    # CU 数据库，自动生成
│   └── runtime/                    # runtime/OpenCode 状态，自动生成
└── opencode/opencode               # 运行节点上的独立可执行程序
```

| 内容 | 本地模式放置位置 | NFS 模式放置位置 | 容器路径 |
| --- | --- | --- | --- |
| source | 运行节点 | NFS 服务端 | `/mnt/source`，只读 |
| common | 运行节点 | NFS 服务端 | `/mnt/common`，只读；桶只挂当前 frontend 和总表 |
| data | 运行节点 | NFS 服务端 | CU 为 `/mnt/env`；桶仅挂本 frontend 工作区 |
| state | 运行节点本地盘 | 运行节点本地盘 | `/data`，读写 |
| opencode | 运行节点本地盘 | 运行节点本地盘 | `/opt/opencode/opencode`，只读可执行 |

业务配置写**容器路径**，例如 `/mnt/common/web-test/app/templates/AGENTS.md`，不能填宿主机 `/data/opencode_backend_k8s/common/...`。模型工具能访问本 frontend 的 app 和获准的用户工作区；config 不开放给用户 Bash。新增 frontend 要分别准备 config、app、data/workspaces 下的目录，不能只在 JSON 中加一项。

### 3.2 提前创建目录

在**运行节点**执行，两种存储模式都需要：

```bash
export DEPLOY_ROOT=/data/opencode_backend_k8s
sudo mkdir -p "$DEPLOY_ROOT"/{state,opencode}
sudo chmod 0755 "$DEPLOY_ROOT" "$DEPLOY_ROOT/opencode"
sudo chmod 0700 "$DEPLOY_ROOT/state"
```

在**文件存储机**执行：本地模式在运行节点；NFS 模式登录 NFS 服务端执行。

```bash
export DEPLOY_ROOT=/data/opencode_backend_k8s
sudo mkdir -p "$DEPLOY_ROOT"/{source,common,data}
sudo mkdir -p "$DEPLOY_ROOT/common/web-test/config"
sudo mkdir -p "$DEPLOY_ROOT/common/web-test/app"/{docs,skills,templates}
sudo mkdir -p "$DEPLOY_ROOT/data/workspaces/web-test"
```

此时目录可以为空，第 5 章构建后再填充文件。

## 4. 存储配置：本地盘或 NFS

### 4.1 方案 A：本地盘

只使用运行节点上的目录。保持 `k8s/opencode/storage.yaml` 中：

```yaml
opencode.io/storage-backend: &storage_backend local
```

核对四个 local PV 的 `hostPath.path` 都指向第 3 章路径。`type: Directory` 要求目录已存在。完成后直接进入第 5 章。

### 4.2 方案 B：NFS 服务端

NFS 只承载 source、common、data。**state 不迁到 NFS**，SQLite WAL 保持本地盘；OpenCode 程序仍通过运行节点 hostPath 挂载。

以下给出 Ubuntu/Debian 的完整例子；已有 NFS 服务可复用导出配置。RHEL 系使用 `nfs-utils` 和 `nfs-server` 服务，按其系统方式安装，不要混用 apt 命令。

**执行位置：NFS 服务端。**第 3.2 节目录必须已创建。将 NODE_IP 换成运行节点访问 NFS 时使用的实际源地址：

```bash
export DEPLOY_ROOT=/data/opencode_backend_k8s
export NODE_IP='REPLACE_WITH_NODE_IP'
sudo apt-get update
sudo apt-get install -y nfs-kernel-server
sudo chown -R 10001:10001 "$DEPLOY_ROOT/source" "$DEPLOY_ROOT/common" "$DEPLOY_ROOT/data"
sudo chmod 0755 "$DEPLOY_ROOT"
sudo chmod -R u+rwX,go-rwx "$DEPLOY_ROOT/common" "$DEPLOY_ROOT/data"
sudo chmod -R a+rX "$DEPLOY_ROOT/source"
sudo install -d /etc/exports.d
sudo tee /etc/exports.d/opencode.exports >/dev/null <<EOF
$DEPLOY_ROOT/source $NODE_IP(ro,sync,no_subtree_check,root_squash,anonuid=10001,anongid=10001)
$DEPLOY_ROOT/common $NODE_IP(ro,sync,no_subtree_check,root_squash,anonuid=10001,anongid=10001)
$DEPLOY_ROOT/data $NODE_IP(rw,sync,no_subtree_check,root_squash,anonuid=10001,anongid=10001)
EOF
sudo systemctl enable --now nfs-kernel-server
sudo exportfs -rav
sudo exportfs -v
```

预期有三个导出目录，source/common 为 ro，data 为 rw。这里将客户端 root 映射到服务端 UID/GID 10001，因此文件要在**服务端本地**赋予 10001 所有权。它与容器以 root 运行并不冲突；不要让默认映射的 65534 去访问仅 10001 可读写的目录。映射参数含义见 [exports(5)](https://man7.org/linux/man-pages/man5/exports.5.html)。

防火墙仅向运行节点开放所用 NFS 协议的端口：NFSv4 通常使用 TCP 2049；NFSv3 还涉及 rpcbind/mountd 等端口，需与服务端实际配置一致。不要向所有来源开放导出。

### 4.3 NFS 客户端验证

**执行位置：运行节点。**安装客户端后逐个验证挂载；NFS_SERVER 是服务端地址：

```bash
export NFS_SERVER='REPLACE_WITH_NFS_SERVER_IP'
sudo apt-get update
sudo apt-get install -y nfs-common
sudo mkdir -p /mnt/opencode-nfs-check/{source,common,data}
sudo mount -t nfs -o ro "$NFS_SERVER:/data/opencode_backend_k8s/source" /mnt/opencode-nfs-check/source
sudo mount -t nfs -o ro "$NFS_SERVER:/data/opencode_backend_k8s/common" /mnt/opencode-nfs-check/common
sudo mount -t nfs "$NFS_SERVER:/data/opencode_backend_k8s/data" /mnt/opencode-nfs-check/data
findmnt -t nfs,nfs4
sudo test -d /mnt/opencode-nfs-check/common/web-test/config
sudo sh -eu -c 'f=$(mktemp /mnt/opencode-nfs-check/data/.write-test.XXXXXX); echo nfs-ok > "$f"; cat "$f"; rm "$f"'
sudo umount /mnt/opencode-nfs-check/source
sudo umount /mnt/opencode-nfs-check/common
sudo umount /mnt/opencode-nfs-check/data
```

预期显示 `nfs-ok` 且均能卸载。只读挂载验证后，第 5 章补齐文件时仍在服务端操作。Kubernetes 会自行挂载 NFS PV，不需要把上述临时挂载写进 fstab。

### 4.4 选择 NFS PV

**执行位置：kubectl 操作机，仓库根目录。**编辑 `k8s/opencode/storage.yaml`：

1. 将唯一的 `&storage_backend local` 改为 `&storage_backend nfs`。
2. 将 `opencode-data-pv-nfs`、`opencode-source-pv-nfs`、`opencode-common-pv-nfs` 三项的 `spec.nfs.server` 全部改成实际服务端地址。
3. 三个 `spec.nfs.path` 分别填写服务端实际导出的 data、source、common 绝对路径。不能填运行节点的临时挂载目录。
4. 保留 `opencode-state-pv-local` 与 state PVC 的 `volumeName`，不要改为 NFS。

上述修改必须在**首次 PVC 绑定前**完成。已绑定的 PVC 不会因修改 selector 自动迁移。本清单同时创建 local 和 NFS PV：选中的四个绑定，未用的三个保持 Available 属正常情况。

## 5. 构建镜像与放置文件

### 5.1 构建并生成交付包

**执行位置：联网 Linux x86_64 构建机，仓库根目录。**需要 Python 3.12、Node.js/npm、Docker 已安装并可用：

```bash
python3 --version
node --version
npm --version
docker version
bash scripts/vendor_opencode_backend.sh
docker build -f Dockerfile.container_up -t agent-container-up:opencode-1.18.31 .
docker build -f Dockerfile.bucket_runtime -t agent-bucket-runtime:opencode-1.18.31-bwrap011 .
# 一并准备第 11 章异步回调测试使用的可选 Web 服务
docker build -f Dockerfile.web_server -t agent-web-server:opencode-1.18.31 .

mkdir -p ../opencode-delivery
docker save -o ../opencode-delivery/agent-images.tar \
  agent-container-up:opencode-1.18.31 \
  agent-bucket-runtime:opencode-1.18.31-bwrap011 \
  agent-web-server:opencode-1.18.31
tar -czf ../opencode-delivery/agent-source.tar.gz \
  --exclude=.git --exclude=.venv --exclude=.opencode-reference \
  --exclude=vendor --exclude='*.tar' --exclude='*.tar.gz' .
cp vendor/opencode ../opencode-delivery/opencode
```

每条命令成功后再继续。vendor 脚本下载并校验 OpenCode 1.18.31、固定 bwrap 包和 Python wheels，再构建插件。**完整镜像构建仍需要网络**，包括基础镜像、apt 和 Chromium 下载；vendor 完成不等于可以全离线构建。

交付包在仓库外的 `../opencode-delivery/`，避免 tar 把自己打进去。确认源码包包含插件产物与依赖：

```bash
tar -tzf ../opencode-delivery/agent-source.tar.gz | grep -E 'opencode_plugin/(dist/index.mjs|dist/permissions.mjs|node_modules/)' | head
ls -lh ../opencode-delivery/
```

三个交付文件分别是镜像包、源码包和独立 OpenCode 程序。镜像不内置业务源码和 OpenCode 程序；漏传后两者无法启动桶。

### 5.2 传输并导入镜像

**执行位置：构建机。**以 SSH/SCP 为例，替换登录用户与地址，目标 `/tmp/opencode-delivery` 用作接收目录：

```bash
ssh USER@NODE_IP 'mkdir -p /tmp/opencode-delivery'
scp ../opencode-delivery/agent-images.tar ../opencode-delivery/opencode USER@NODE_IP:/tmp/opencode-delivery/
# STORAGE_HOST：本地模式填运行节点；NFS 模式填 NFS 服务端
ssh USER@STORAGE_HOST 'mkdir -p /tmp/opencode-delivery'
scp ../opencode-delivery/agent-source.tar.gz USER@STORAGE_HOST:/tmp/opencode-delivery/
```

kubectl 操作机还需持有同一版本仓库的 `k8s/opencode/` 清单及其现场修改。构建机兼操作机时直接使用当前仓库。

**执行位置：运行节点。**根据 Kubernetes 实际运行时选择一条导入，不要三条全跑：

```bash
# 标准 containerd
sudo ctr -n k8s.io images import /tmp/opencode-delivery/agent-images.tar
sudo ctr -n k8s.io images list | grep agent-
# K3s：用以下命令替代标准 containerd 命令
# sudo k3s ctr images import /tmp/opencode-delivery/agent-images.tar
# 使用 Docker 运行时的集群：
# docker load -i /tmp/opencode-delivery/agent-images.tar
```

在 kubectl 操作机可用下列命令确认节点运行时；仅在 Docker 中 load，但集群使用 containerd，会导致镜像不可用：

```bash
kubectl get node "$NODE" -o jsonpath='{.status.nodeInfo.containerRuntimeVersion}{"\n"}'
```

也可推送到内网仓库。使用仓库地址时须同时修改 `backend.yaml` 的 CU image、实际 `common/frontends.json` 的 `control.bucket_pool.image` 和可选 `web-server.yaml` 的 image。私有仓库认证须对动态 bucket 使用的 ServiceAccount/节点也生效，仅给 CU 配拉取凭据不够。

### 5.3 安装源码、二进制和配置

以下复制示例用于**首次部署**。已有环境按第 8 章更新，不用示例覆盖业务配置。

**执行位置：文件存储机。**解包并初始化 frontend：

```bash
export DEPLOY_ROOT=/data/opencode_backend_k8s
sudo tar -xzf /tmp/opencode-delivery/agent-source.tar.gz -C "$DEPLOY_ROOT/source"
sudo cp -n "$DEPLOY_ROOT/source/config/opencode/frontends.json" "$DEPLOY_ROOT/common/"
sudo cp -rn "$DEPLOY_ROOT/source/config/opencode/web-test/config/." "$DEPLOY_ROOT/common/web-test/config/"
sudo cp -rn "$DEPLOY_ROOT/source/config/opencode/web-test/app/." "$DEPLOY_ROOT/common/web-test/app/"
if ! sudo test -f "$DEPLOY_ROOT/common/web-test/app/templates/AGENTS.md"; then
  printf '%s\n' '使用中文回复。生成文件保存在当前用户工作目录，交付文件时调用 publish_attachment。' \
    | sudo tee "$DEPLOY_ROOT/common/web-test/app/templates/AGENTS.md" >/dev/null
fi
sudoedit "$DEPLOY_ROOT/common/frontends.json"
sudoedit "$DEPLOY_ROOT/common/web-test/config/opencode-server.json"
sudoedit "$DEPLOY_ROOT/common/web-test/config/user-permissions.yaml"
```

必须填写以下配置，不能保留 `REPLACE`、`your-model`、`your-frontend` 等占位值：

| 文件 | 配置项 | 填写方式 |
| --- | --- | --- |
| frontends.json | `frontends[].frontend_id` | 本文为 `web-test`，目录名与 API 路径一致，字段不是 `id` |
| frontends.json | `control.bucket_pool` | 镜像、namespace、PVC、node_selector、opencode_host_path 与部署清单一致；默认值见第 1 章及 config.py |
| frontends.json | `frontends[].send_msg_url` | 实际业务回调；第 11 章 Web 测试填 `http://web-server:8000/outbound` |
| frontends.json | `frontends[].attachment_storage` | 实际 MinIO endpoint、access_key、secret_key、bucket、public_base_url |
| opencode-server.json | `provider.intranet.options` | 模型服务 baseURL 与 apiKey |
| opencode-server.json | `provider.intranet.models`、`model` | models 中定义实际模型 ID；model 填 `intranet/实际模型ID` |
| opencode-server.json | `instructions`、`skills.paths` | 保持指向本 frontend 的 `/mnt/common/web-test/app/` 下实际目录/文件 |
| user-permissions.yaml | `sandbox.enabled`、`session.enabled` | 必须均为 true；下载附件还需允许 sandbox.network |
| session-logging.json | 会话日志开关 | 按 [日志说明](SESSION_LOGGING.md) 配置 |

MinIO 的 `attachments` bucket 需预先创建并授权。`public_base_url` 非空时返回拼接的未签名链接，需能直接下载；为空时返回基于 endpoint 的预签名 URL，因此 endpoint 必须对下载方可达。自建 MinIO 参考 `k8s/minio/minio.yaml`，其中节点与凭据需要另行调整。

配置好后在**文件存储机本地**执行权限准备与文件检查（NFS 不要从客户端 chown）：

```bash
sudo chown -R 10001:10001 "$DEPLOY_ROOT/source" "$DEPLOY_ROOT/common" "$DEPLOY_ROOT/data"
sudo chmod 0755 "$DEPLOY_ROOT"
sudo chmod -R a+rX "$DEPLOY_ROOT/source"
sudo chmod -R u+rwX,go-rwx "$DEPLOY_ROOT/common" "$DEPLOY_ROOT/data"
sudo test -f "$DEPLOY_ROOT/source/opencode_plugin/dist/index.mjs"
sudo test -f "$DEPLOY_ROOT/source/opencode_plugin/dist/permissions.mjs"
sudo test -d "$DEPLOY_ROOT/source/opencode_plugin/node_modules"
sudo test -r "$DEPLOY_ROOT/common/frontends.json"
sudo test -r "$DEPLOY_ROOT/common/web-test/app/templates/AGENTS.md"
```

以上 test 成功时无输出。source 可读是为了兼容 UID 10001 的 Web 服务；凭据只放 common/config，不放源码中。NFS 映射使用 10001，本地 root 同样能访问这些目录。

**执行位置：运行节点。**安装独立二进制并验证：

```bash
export DEPLOY_ROOT=/data/opencode_backend_k8s
sudo install -m 0555 /tmp/opencode-delivery/opencode "$DEPLOY_ROOT/opencode/opencode.new"
sudo mv -f "$DEPLOY_ROOT/opencode/opencode.new" "$DEPLOY_ROOT/opencode/opencode"
"$DEPLOY_ROOT/opencode/opencode" --version
sudo test -w "$DEPLOY_ROOT/state"
```

预期 OpenCode 版本为 1.18.31。原子 rename 避免直接覆盖运行中的可执行文件。

### 5.4 可选：已有旧镜像的 bwrap 兼容修复

bucket 镜像现在固定使用 Debian `bubblewrap=0.11.0-2+deb13u1`，从 Debian Snapshot 下载并校验 SHA256，安装后设置 hold。完整构建和下面的快速修复都使用同一包。新版 `0.12.0-1~deb13u1` 关闭了旧内核兼容路径，4.19 节点可能报 `Can't mkdir parents for /proc: Function not implemented`；root/privileged 不能补齐这个系统调用。

若已有原始 OpenCode bucket 镜像，无需重建 Python、Chromium 等依赖。在联网 Docker 构建机的源码根目录执行：

```bash
bash scripts/vendor_bubblewrap.sh
docker build -f Dockerfile.bucket_runtime.compat \
  --build-arg RUNTIME_IMAGE=agent-bucket-runtime:opencode-1.18.31 \
  -t agent-bucket-runtime:opencode-1.18.31-bwrap011 .
docker run --rm --entrypoint sh agent-bucket-runtime:opencode-1.18.31-bwrap011 \
  -c 'id; bwrap --version; dpkg-query -W bubblewrap; apt-mark showhold'
docker save -o bucket-bwrap011.tar agent-bucket-runtime:opencode-1.18.31-bwrap011
```

预期用户为 root，bwrap 显示 0.11.0，Debian 包版本为 0.11.0-2+deb13u1，hold 列表包含 bubblewrap。镜像包传到实际运行节点后导入（K3s 使用 `k3s ctr`）：

```bash
sudo ctr -n k8s.io images import bucket-bwrap011.tar
```

修改服务器 `/data/opencode_backend_k8s/common/frontends.json` 中 `control.bucket_pool.image` 为下面的值；只改 image，不要覆盖现有模型/渠道/存储配置：

```json
"image": "agent-bucket-runtime:opencode-1.18.31-bwrap011"
```

暂停业务提交后更新服务。CU 重启读取新镜像配置；已有 bucket 使用 set image 更新，不能只 rollout restart 旧镜像：

```bash
kubectl -n opencode-sv rollout restart deployment/container-up
kubectl -n opencode-sv rollout status deployment/container-up --timeout=180s
kubectl -n opencode-sv set image deployment -l app=opencode-bucket \
  runtime=agent-bucket-runtime:opencode-1.18.31-bwrap011
kubectl -n opencode-sv get pods -l app=opencode-bucket
kubectl -n opencode-sv logs -f -l app=opencode-bucket \
  --all-containers=true --prefix=true --tail=100 --max-log-requests=20
```

2026-10-14 本地验证：在 WSL Docker 中对 openat2 返回 ENOSYS（模拟旧内核缺少该调用），原 0.12.0 包复现相同 `/proc: Function not implemented` 错误，0.11.0 包通过项目实际 `SandboxExecutor.probe`。该对照不是目标 4.19 节点的完整验收，部署后仍须运行第 11 章接口测试。

旧版不包含 0.12.0 的符号链接遍历安全修复；本版本按当前旧内核兼容需求固定，不应将其视为最新版安全基线。包记录见 [Debian](https://tracker.debian.org/news/1790935/accepted-bubblewrap-0120-1deb13u1-source-into-stable-security/)。

## 6. 启动服务

### 6.1 应用存储与 CU 清单

**执行位置：kubectl 操作机，持有已修改清单的仓库根目录。**

```bash
kubectl apply -f k8s/opencode/storage.yaml
kubectl -n opencode-sv get pvc
kubectl get pv
```

先确认四个 PVC 都为 Bound。若 Pending，停在这里按第 10 章检查 selector、PV claimRef 和容量。不要通过反复创建 Pod 处理 PVC 绑定问题。

```bash
kubectl apply -f k8s/opencode/backend.yaml
kubectl -n opencode-sv rollout status deployment/container-up --timeout=180s
kubectl -n opencode-sv get pods,deployments,svc,pvc -o wide
kubectl -n opencode-sv logs deployment/container-up -c control --tail=100
kubectl -n opencode-sv logs deployment/container-up -c gateway --tail=100
kubectl -n opencode-sv exec deployment/container-up -c control -- \
  python -m container_up --check-config --config /mnt/common/frontends.json
curl -fsS "$GATEWAY_URL/health/live"
curl -fsS "$GATEWAY_URL/health/ready"
```

预期 `container-up` Pod 为 Running 且 Ready 为 2/2，Gateway Service 的 NodePort 为 30081，配置检查成功、健康接口返回成功。CU 8001、Gateway 8000 及内部通信地址使用代码默认值，无需写入总表；若实际配置中残留旧的 `gateway` 块或显式 `control.port`，应移除。若容器不能启动，先看对应日志修配置，无法对退出的容器 exec。

此 Deployment 必须保持一个副本，Gateway 和 CU 各自 SQLite 使用单写进程；它们随 Pod 一起更新和启停。此时没有 bucket 属正常情况；第 11 章首个请求才验证建桶和模型调用。**Pod Ready 只代表服务就绪，不代表模型、bwrap 和附件链路已通过。**

### 6.2 可选 Web 与业务 MCP

使用第 11 章异步回调测试时，确认 send_msg_url 已填 `http://web-server:8000/outbound`，然后：

```bash
kubectl apply -f k8s/opencode/web-server.yaml
kubectl -n opencode-sv rollout status deployment/web-server --timeout=180s
```

如果启动后才改回调地址，需要重启 Gateway/CU Pod 重新读取。工程场景和隐患专家 MCP 分别有独立清单 `engineering-scene-mcp.yaml`、`hazard-expert-mcp.yaml`，需要各自镜像和配置，不属于最小部署。隐患专家接入步骤见 [其 README](../hazard_expert_mcp/README.md)，不要注册成同一后端的模型工具导致递归调用。

首次部署完成后直接去第 11 章；以下章节按需使用。

## 7. 关闭与恢复服务

### 7.1 停服但保留数据

**执行位置：kubectl 操作机。**先停止上游业务提交，等待执行中的请求结束；强行停服会中断任务。先停 CU，确认退出后再停 bucket，避免 CU 在停服过程中重新拉起桶。

```bash
kubectl -n opencode-sv scale deployment/container-up --replicas=0
kubectl -n opencode-sv wait --for=delete pod -l app=container-up --timeout=180s
for d in $(kubectl -n opencode-sv get deployment -l app=opencode-bucket -o name); do
  kubectl -n opencode-sv scale "$d" --replicas=0
done
kubectl -n opencode-sv get pods -o wide
```

若部署了 Web/MCP，也需停止这些入口。对于本项目专用 namespace，以下命令将其中**全部 Deployment** 缩到 0：

```bash
kubectl -n opencode-sv scale deployment --all --replicas=0
kubectl -n opencode-sv get deployments
kubectl -n opencode-sv get pods
```

等全部 Pod 退出后，才可做离线文件备份。PVC、PV、源码、配置、工作区、历史会话和数据库均保留。不要仅 delete Pod，Deployment 会马上重建它。

### 7.2 恢复服务

```bash
kubectl -n opencode-sv scale deployment/container-up --replicas=1
kubectl -n opencode-sv rollout status deployment/container-up --timeout=180s
# 仅在之前部署过 Web 时执行
# kubectl -n opencode-sv scale deployment/web-server --replicas=1
curl -fsS "$GATEWAY_URL/health/ready"
```

按需恢复原先部署的 MCP。bucket 留在 0，由 CU 按后续请求分配和拉起；不要把全部历史桶盲目缩放为 1。

## 8. 更新服务

### 8.1 判断需要更新什么

| 改动 | 要做的事 |
| --- | --- |
| Python 业务源码 | 同步 source，重启受影响服务 |
| 插件 | 重新 npm ci/build，同步 dist、node_modules、package 文件，重启 bucket |
| skills/模板/规则 | 更新实际 common/<frontend>/app；涉及启动配置时重启相关桶 |
| 总表 frontends.json | 更新实际 common 文件，重启 CU 及相关桶 |
| 模型/权限配置 | 更新该 frontend 的 config，处理已有会话后重启该 frontend 的桶 |
| Python/系统依赖 | 重建镜像，使用新标签，导入或推送，修改清单和实际总表 |
| bucket 镜像、挂载、启动参数 | 更新总表/源码，并重新生成已有 bucket Deployment；仅 restart 不会改 Pod 模板 |
| OpenCode 程序 | 使用第 5.3 节 `.new` + rename 替换，重启 bucket |

更新前停止提交、等待任务结束，再按第 7 章停服。备份 state 时必须停止所有写入者，复制整个状态目录；运行中的单个 SQLite db 文件不能作为一致备份。更新不需要删除 state。

### 8.2 同步源码与恢复

在构建机新版本仓库根目录重建插件并重新生成第 5.1 节源码包，传到文件存储机：

```bash
npm ci --prefix opencode_plugin
npm run build --prefix opencode_plugin
```

在文件存储机解包到 source，重新执行第 5.3 节权限检查。不要将仓库示例 config 覆盖到实际 common；业务配置、skills 和模板按需要单独更新。解包不会清理旧文件，版本确有删文件时按差异清理。

按第 7.2 节恢复。若未停服，仅在确认没有执行中任务的维护窗口重启已运行服务：

```bash
kubectl -n opencode-sv rollout restart deployment/container-up
kubectl -n opencode-sv rollout status deployment/container-up --timeout=180s
for d in $(kubectl -n opencode-sv get deployment -l app=opencode-bucket -o name); do
  kubectl -n opencode-sv rollout restart "$d"
done
```

### 8.3 更新已有 bucket 的 Deployment

以下在**装有项目 Python 依赖和 kubectl 的管理环境**运行，从新版源码根目录执行，配置文件使用从实际 common 取得的当前总表（可能含凭据，存放于受限位置）。运行节点使用 NFS 时，本地路径未必有 common，不能照抄不存在的路径。

```bash
export ACTUAL_CONFIG=/absolute/path/to/current/frontends.json
python3 -m scripts.render_bucket_updates --config "$ACTUAL_CONFIG" > /tmp/opencode-bucket-updates.yaml
kubectl diff -f /tmp/opencode-bucket-updates.yaml
# diff 返回 1 表示有差异；核对后应用，工具执行失败则不要应用输出文件
kubectl apply -f /tmp/opencode-bucket-updates.yaml
kubectl apply -f k8s/opencode/backend.yaml
```

工具要求 bucket 有明确的 `opencode.io/frontend-id` annotation，保留当前副本数。旧统一启动器升级也需要重新生成模板；CU 启动入口是 `python -m container_up`，bucket 是 `python -m bucket_runtime`，`agent_backend` 仅为库。

session 权限变化不能只靠重启修复：先完成对应 frontend 的整桶回收，再更新配置，下一次分配创建新 session；旧 session 权限回读不一致会拒绝执行。会话迁移与历史保留限制见 [日志与会话说明](SESSION_LOGGING.md)。

## 9. 删除命名空间与重装

### 9.1 整套卸载

**这会停止 namespace 中所有服务并删除 PVC。**先停止上游提交、确认任务结束并备份需要保留的状态。以下仅删除 Kubernetes 对象；PV 回收策略是 Retain，不删除宿主机/NFS 文件，也不删除独立 MinIO 服务或附件。

```bash
kubectl delete namespace opencode-sv --ignore-not-found --wait=true --timeout=180s
```

等 namespace 删除成功后再删除本项目七个 PV（PV 是集群资源，不随 namespace 删除）：

```bash
kubectl delete pv \
  opencode-data-pv-local opencode-source-pv-local opencode-common-pv-local \
  opencode-state-pv-local \
  opencode-data-pv-nfs opencode-source-pv-nfs opencode-common-pv-nfs \
  --ignore-not-found --wait=true --timeout=180s
kubectl get namespace opencode-sv
kubectl get pv
```

namespace 返回 NotFound 属预期。若超时，检查残留 Pod/PVC 与事件，不直接强删 finalizer：

```bash
kubectl -n opencode-sv get pods,pvc
kubectl -n opencode-sv get events --sort-by=.metadata.creationTimestamp
```

### 9.2 保留磁盘数据重新部署

确认上述 namespace 和 PV 对象已删除，原来的目录和配置仍在。保持原存储方案，按第 6 章重新 apply storage.yaml、backend.yaml，并恢复所需可选服务。

Retain PV 在旧 PVC 删除后可能为 Released，仍带旧 claimRef；仅重建同名 PVC 不会自动绑定旧卷，所以重装流程明确删除并重建本项目 PV 对象。不要操作其他项目的 PV。

保留的 state 仍有请求 ID、历史会话和 bucket 记录，因此这是重装服务，**不是清空业务数据**。从 local 切换到 NFS 还需要停服后迁移 source/common/data 并验证权限，不能只改 selector。

## 10. 日志与排障

### 10.1 Gateway、CU 与所有 bucket 日志

**执行位置：kubectl 操作机。**

```bash
# CU 最近日志 / 持续跟踪，Ctrl+C 只结束跟踪
kubectl -n opencode-sv logs deployment/container-up -c control --tail=200 --timestamps
kubectl -n opencode-sv logs -f deployment/container-up -c control --tail=100 --timestamps
# Gateway 渠道接入与外发日志
kubectl -n opencode-sv logs -f deployment/container-up -c gateway --tail=100 --timestamps
# 所有当前 bucket；首次未建桶或全部回收时可能没有匹配项
kubectl -n opencode-sv logs -f -l app=opencode-bucket -c runtime \
  --prefix=true --timestamps --tail=100 --max-log-requests=20
# 可选异步回调接收器
kubectl -n opencode-sv logs deployment/web-server --tail=100 --timestamps
```

bucket 的 runtime stdout 包含 OpenCode 与会话过程日志；日志开关在 frontend 的 `config/session-logging.json`，字段说明见 [SESSION_LOGGING.md](SESSION_LOGGING.md)。超过 20 个 Pod 时调整 max-log-requests，或直接选单个 Pod。

### 10.2 单个 Pod、崩溃日志与事件

```bash
kubectl -n opencode-sv get pods -o wide
export POD='REPLACE_WITH_ACTUAL_POD_NAME'
kubectl -n opencode-sv describe pod "$POD"
kubectl -n opencode-sv logs "$POD" --all-containers=true --tail=200 --timestamps
# 同一 Pod 内上一次容器崩溃的日志；不存在上一个实例时会报错
kubectl -n opencode-sv logs "$POD" --all-containers=true --previous --tail=200
kubectl -n opencode-sv get events --sort-by=.metadata.creationTimestamp
kubectl -n opencode-sv describe pvc
kubectl get nodes -L opencode-runtime
```

`--previous` 无法读取已经删除的旧 Pod；长期留存需要集群日志采集。不要把含凭据的完整配置贴到日志里排查。

| 现象 | 先检查什么 |
| --- | --- |
| PVC Pending | storage_backend、selector、PV Released/claimRef、容量匹配 |
| Pod Pending | 节点标签是否唯一、污点、资源、PV 节点亲和性 |
| FailedMount | hostPath/subPath 是否存在；NFS server/path、客户端、网络及导出权限 |
| ImagePullBackOff | 镜像是否导入实际运行时；镜像名称、标签和仓库认证 |
| Permission denied | 本地 state 可写、二进制执行位；NFS root_squash 映射与服务端 UID |
| CU Ready 但建桶失败 | runtime 日志、插件 dist/node_modules、OpenCode 二进制、bwrap 探测 |
| bwrap Function not implemented | 旧内核与 bwrap 版本，见第 5.4 节；privileged 不能补系统调用 |
| 404 frontend is not registered | 总表 frontend_id 与请求路径是否一致 |
| 422 | chat_id 缺失/为空、旧 raw 字段、未知字段或类型错误 |
| 409 | 同 request_id 输入不同，或同步/异步模式冲突 |
| 504 / curl 超时 | 按原 request_id 查询；客户端超时不等于任务已取消 |
| 附件 URL 无法下载 | MinIO 授权、bucket、endpoint/public_base_url 可达性、签名有效期 |
| 更新后 EXECUTION_INTERRUPTED | 执行被重启打断，先检查副作用再决定是否重提 |

## 11. 测试命令

**执行位置：能访问 Gateway NodePort 的 Linux Bash 终端。**以下 curl 使用可直接替换的完整 URL，不依赖前面终端的变量。统一将 `192.168.199.168:30081` 替换为实际节点地址，frontend 保持与总表一致。

每轮测试更换 request_id；同一轮去重测试则故意复用原 ID。想重新验证会话记忆时，chat_id 也换新值。先完成同步和会话，再验证异步及附件。

### 11.1 同步消息与会话

以下命令在 Bash 中直接执行，不需要 jq、环境变量或请求文件。将 `192.168.199.168:30081` 替换为实际 CU 节点地址；`web-test` 必须是已注册的 frontend。示例 IP 仅作占位，不表示已验证该服务器。

发送消息：

```bash
curl -i -sS --max-time 660 -X POST 'http://192.168.199.168:30081/inbound-sync/web-test' \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "deploy-smoke",
    "chat_id": "smoke-chat-001",
    "request_id": "smoke-request-001",
    "content": "请记住测试代号蓝鲸472，并只回复已记住。",
    "attachments": [],
    "metadata": {}
  }'
```

预期 HTTP 200，响应 `status` 为 `completed`，`content` 为模型回复。首次请求会自动建桶；HTTP 200 也可能带 failed/cancelled，需检查响应。同步请求不触发回调，delivery_state 为 pending 不代表执行失败。每次新测试换一个 request_id；原样重发同一 ID 只返回已有请求结果。

继续同一会话：

```bash
curl -i -sS --max-time 660 -X POST 'http://192.168.199.168:30081/inbound-sync/web-test' \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "deploy-smoke",
    "chat_id": "smoke-chat-001",
    "request_id": "smoke-request-002",
    "content": "刚才让你记住的测试代号是什么？",
    "attachments": [],
    "metadata": {}
  }'
```

预期回复包含蓝鲸472。更换 chat_id 开始另一会话，但仍共享同一用户工作目录。chat_id 必填，不要传旧 raw 字段。

### 11.2 查询、去重和冲突

查询第 11.1 节请求：

```bash
curl -i -sS 'http://192.168.199.168:30081/requests/web-test/smoke-request-001'
```

去重测试：原样再次执行第 11.1 节第一条 curl，预期返回相同请求结果，不重复执行。冲突测试：保留 ID、修改 content，预期 HTTP 409：

```bash
curl -i -sS -X POST 'http://192.168.199.168:30081/inbound-sync/web-test' \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "deploy-smoke",
    "chat_id": "smoke-chat-001",
    "request_id": "smoke-request-001",
    "content": "这是一条不同的输入",
    "attachments": [],
    "metadata": {}
  }'
```

同步和异步不能共用同一个 request_id。CU 默认等待期限为 600 秒，超时返回 504，detail 包含 request_id/error；客户端超时也不代表任务停止。按原 ID 查询，不要换 ID 重发有副作用的任务。

### 11.3 异步消息和回调

先配置 frontend 的 send_msg_url 为实际业务回调。使用示例 Web 代理接收时，填 `http://web-server:8000/outbound`，在无执行中任务时执行：

```bash
kubectl apply -f k8s/opencode/web-server.yaml
kubectl -n opencode-sv rollout status deployment/web-server --timeout=180s
kubectl -n opencode-sv rollout restart deployment/container-up
kubectl -n opencode-sv rollout status deployment/container-up --timeout=180s
```

异步发送：

```bash
curl -i -sS -X POST 'http://192.168.199.168:30081/inbound/web-test' \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "deploy-smoke",
    "chat_id": "async-chat-001",
    "request_id": "async-request-001",
    "content": "请回复异步链路测试成功",
    "attachments": [],
    "metadata": {}
  }'
```

首次响应通常为 queued。过几秒执行下面的查询，尚未完成则再执行一次：

```bash
curl -i -sS 'http://192.168.199.168:30081/requests/web-test/async-request-001'
```

预期最终 status=completed、delivery_state=sent。unknown 表示投递结果不确定，服务不会盲目重发。使用 Web 代理时查看回调日志：

```bash
kubectl -n opencode-sv logs deployment/web-server --tail=100
```

预期出现 `Outbound message received`。Web `/inbound` 是异步代理，同步请求直接访问 CU。

### 11.4 取消请求

先提交一个耗时请求：

```bash
curl -i -sS -X POST 'http://192.168.199.168:30081/inbound/web-test' \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "deploy-cancel",
    "chat_id": "cancel-chat-001",
    "request_id": "cancel-request-001",
    "content": "请用 bash 执行 sleep 60，然后回复结束。",
    "attachments": [],
    "metadata": {}
  }'
```

取消该请求：

```bash
curl -i -sS -X POST 'http://192.168.199.168:30081/requests/web-test/cancel-request-001/cancel'
```

查询最终状态：

```bash
curl -i -sS 'http://192.168.199.168:30081/requests/web-test/cancel-request-001'
```

取消接口先返回当前状态，稍后查询应为 cancelled。如果已经完成，不覆盖终态。要验证执行中的进程终止，先从 bucket 日志确认 sleep 已启动再取消。

### 11.5 文件发布和入站附件

生成文件并发布到 MinIO：

```bash
curl -i -sS --max-time 660 -X POST 'http://192.168.199.168:30081/inbound-sync/web-test' \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "deploy-smoke",
    "chat_id": "file-chat-001",
    "request_id": "file-request-001",
    "content": "请用 bash 在当前工作目录的 deploy-smoke.txt 写入 opencode-file-ok，然后必须调用 publish_attachment 发布这个文件。",
    "attachments": [],
    "metadata": {}
  }'
```

预期 status=completed，attachments 中有 url。复制实际返回的完整 URL 替换下面的地址（签名参数也须保留），下载并直接在终端显示内容：

```bash
curl -i -sS 'https://REPLACE_WITH_RETURNED_ATTACHMENT_URL'
```

预期 HTTP 200，正文含 opencode-file-ok。仅模型正文出现本地文件路径不算发布成功。

入站附件测试：把下面 url 替换为刚返回的实际文件 URL：

```bash
curl -i -sS --max-time 660 -X POST 'http://192.168.199.168:30081/inbound-sync/web-test' \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "deploy-smoke",
    "chat_id": "attachment-chat-001",
    "request_id": "attachment-request-001",
    "content": "请读取附件并回复其原文内容。",
    "attachments": [
      {
        "url": "https://REPLACE_WITH_RETURNED_ATTACHMENT_URL",
        "filename": "deploy-smoke.txt"
      }
    ],
    "metadata": {}
  }'
```

CU/runtime 只转发附件引用，模型需要通过获准工具获取 URL，桶网络须能够访问下载地址。

### 11.6 开发侧回归测试

以下在独立开发环境运行，不是集群部署验收的替代：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest tests -q
npm ci --prefix opencode_plugin
npm run build --prefix opencode_plugin
npm test --prefix opencode_plugin
# Linux 下使用准备好的真实二进制；模型服务由测试模拟
OPENCODE_TEST_BINARY="$PWD/vendor/opencode" python -m pytest tests -q
```

验收至少完成：Gateway/CU Pod Ready、请求自动建桶、同步 completed、同会话上下文、去重/409、附件下载；使用异步业务时还需验证 delivery_state=sent 和业务回调日志。本文命令按仓库实现编写，实际模型、MinIO 和集群能力需在目标环境运行验证。
