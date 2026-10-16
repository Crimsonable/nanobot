# OpenCode 分支部署与测试手册

适用于 `codex/opencode-backend`。命令在 **Linux x86_64、Bash** 中执行，从仓库根目录开始；不是 Windows PowerShell 命令。本文以已有 Kubernetes 集群、一个存储/运行节点、`local` 存储和 `web-test` frontend 为主线。CU 收到请求后自动创建 bucket，不需要手工部署 runtime。

旧 `k8s-merge-nanobot` 的配置、数据库和接口不能直接当作本版本使用。首次部署使用独立目录 `/data/opencode_backend_k8s` 和命名空间 `opencode-sv`，不覆盖旧服务数据。更详细的存储说明见 [K8s 说明](../k8s/opencode/README.md)。

## 当前 root + privileged 联调配置

当前 CU 和动态 bucket 均以 UID/GID 0 运行并开启 privileged；Dockerfile 默认用户也改为 root。Kubernetes 配置会覆盖已有镜像的 USER，因此这次联调无需重建镜像。此前第 3 节的 UID 10001 权限准备可用于后续恢复非 root 运行，不是当前 root 模式的必需步骤；OpenCode 文件仍须有执行位。

在服务器同步最新 `source/container_up/bucket_manager.py` 和 `k8s/opencode/backend.yaml` 后执行。先暂停业务提交，以下操作会重建 Pod：

```bash
kubectl apply -f k8s/opencode/backend.yaml
kubectl -n opencode-sv rollout restart deployment/container-up
kubectl -n opencode-sv rollout status deployment/container-up --timeout=180s

# 更新已存在的 bucket；新建 bucket 自动使用最新源码中的配置。
for deployment in $(kubectl -n opencode-sv get deployments -l app=opencode-bucket -o name); do
  kubectl -n opencode-sv patch "$deployment" --type=strategic -p '{"spec":{"template":{"spec":{"securityContext":{"runAsUser":0,"runAsGroup":0,"fsGroup":0},"containers":[{"name":"runtime","securityContext":{"privileged":true,"allowPrivilegeEscalation":true,"readOnlyRootFilesystem":false,"capabilities":null}}]}}}}'
done
kubectl -n opencode-sv get pods -o wide
```

privileged 扩大了容器对宿主机的访问权限，本节用于当前联调。它不能补齐旧内核缺少的系统调用；若 bubblewrap 仍报 Function not implemented，按第 2.1 节替换为固定的旧版 bwrap 镜像。

## 1. 环境与变量

联网构建机需要 Python 3.12、Node.js/npm、curl、Docker；部署节点需要 rsync，操作机需要 kubectl、curl。构建机和节点可以是同一台。以下变量在后续各终端中按需重新设置：

```bash
export DEPLOY_ROOT=/data/opencode_backend_k8s
export NS=opencode-sv
export NODE=ANQUANZHOUQI-168
export FRONTEND=web-test
export CU_URL=http://192.168.199.168:30082

git branch --show-current
kubectl get nodes -o wide
kubectl get nodes -L opencode-runtime
docker version
python3 --version
node --version
npm --version
```

将 ANQUANZHOUQI-168、CU_URL 替换为实际值。默认清单只应有一个节点标记 `opencode-runtime=true`，该节点保存本地卷和 OpenCode 二进制。节点须支持非特权 user namespace，seccomp/AppArmor 配置须允许 bwrap；runtime 启动会实测，失败时不会关闭隔离继续运行。

## 2. 构建镜像和离线资源

在联网 Linux 构建机的仓库根目录执行：

```bash
bash scripts/vendor_opencode_backend.sh
docker build -f Dockerfile.container_up -t agent-container-up:opencode-1.18.31 .
docker build -f Dockerfile.bucket_runtime -t agent-bucket-runtime:opencode-1.18.31-bwrap011 .
# 需要异步回调测试时构建可选 Web 代理
docker build -f Dockerfile.web_server -t agent-web-server:opencode-1.18.31 .

docker save -o agent-images.tar \
  agent-container-up:opencode-1.18.31 \
  agent-bucket-runtime:opencode-1.18.31-bwrap011 \
  agent-web-server:opencode-1.18.31
tar -czf agent-source.tar.gz \
  --exclude=.git --exclude=.venv --exclude=.opencode-reference \
  --exclude=vendor --exclude=agent-images.tar --exclude=agent-source.tar.gz .
```

vendor 脚本下载并校验 OpenCode 1.18.31、下载 Python wheels、构建插件。源码包必须保留 `opencode_plugin/dist` 和 `node_modules`。将两个 tar 包和 `vendor/opencode` 传到部署节点；若机器分离，也将当前仓库的部署清单带到 kubectl 操作机。

离线节点按实际容器运行时选择一种导入方式，不能只导入到未被 Kubernetes 使用的 Docker：

```bash
# 标准 containerd
sudo ctr -n k8s.io images import agent-images.tar
sudo ctr -n k8s.io images list | grep agent-

# K3s 使用这条代替上面的 ctr
# sudo k3s ctr images import agent-images.tar

# 使用 Docker 运行时的节点
# docker load -i agent-images.tar
```

也可以推送到内网镜像仓库；此时同步修改 `k8s/opencode/backend.yaml` 的 CU 镜像、`common/frontends.json` 的 `control.bucket_pool.image`，以及可选 Web 清单的镜像。使用私有仓库时还需按集群已有方式配置拉取凭据。

### 2.1 已有镜像的 bwrap 旧版修复（4.19 节点）

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

2026-10-14 本地验证：在 WSL Docker 中对 openat2 返回 ENOSYS（模拟旧内核缺少该调用），原 0.12.0 包复现相同 `/proc: Function not implemented` 错误，0.11.0 包通过项目实际 `SandboxExecutor.probe`。该对照不是目标 4.19 节点的完整验收，部署后仍须运行第 6 节接口测试。

旧版不包含 0.12.0 的符号链接遍历安全修复；本版本按当前旧内核兼容需求固定，不应将其视为最新版安全基线。包记录见 [Debian](https://tracker.debian.org/news/1790935/accepted-bubblewrap-0120-1deb13u1-source-into-stable-security/)。

## 3. 存储节点首次初始化

以下命令在实际存储节点执行。配置复制只用于首次部署；已有环境不要用示例覆盖正在使用的配置。

```bash
sudo mkdir -p "$DEPLOY_ROOT"/{source,common,data,state,opencode}
sudo tar -xzf agent-source.tar.gz -C "$DEPLOY_ROOT/source"
sudo mkdir -p "$DEPLOY_ROOT/common/web-test/app"/{docs,skills,templates}
sudo mkdir -p "$DEPLOY_ROOT/common/web-test/config"
sudo mkdir -p "$DEPLOY_ROOT/data/workspaces/web-test"
sudo cp -n "$DEPLOY_ROOT/source/config/opencode/frontends.json" "$DEPLOY_ROOT/common/"
sudo cp -rn "$DEPLOY_ROOT/source/config/opencode/web-test/config/." "$DEPLOY_ROOT/common/web-test/config/"
sudo cp -rn "$DEPLOY_ROOT/source/config/opencode/web-test/app/." "$DEPLOY_ROOT/common/web-test/app/"

# opencode 为传到本机的 vendor/opencode；按实际接收位置改路径
sudo install -m 0555 ./vendor/opencode "$DEPLOY_ROOT/opencode/opencode.new"
sudo mv -f "$DEPLOY_ROOT/opencode/opencode.new" "$DEPLOY_ROOT/opencode/opencode"
```

示例配置引用 `app/templates/AGENTS.md`。如果没有业务模板，先创建最小规则文件，已有文件则保留：

```bash
if ! sudo test -f "$DEPLOY_ROOT/common/web-test/app/templates/AGENTS.md"; then
  printf '%s\n' '使用中文回复。生成文件保存在当前用户工作目录，交付文件时调用 publish_attachment。' \
    | sudo tee "$DEPLOY_ROOT/common/web-test/app/templates/AGENTS.md" >/dev/null
fi
```

**文件复制完成后，在存储节点完整执行下面这一段，统一设置全部目录和文件权限。**首次部署和以后重新复制文件后都执行这一段。路径固定写全，不依赖前面终端的变量；使用自定义部署目录时统一替换。宿主机无需创建 10001 用户。

```bash
sudo bash <<'BASH'
set -euo pipefail
ROOT=/data/opencode_backend_k8s

# 确认关键文件已复制；缺失时停止，不输出虚假的成功信息。
test -f "$ROOT/source/opencode_plugin/dist/index.mjs"
test -d "$ROOT/source/opencode_plugin/node_modules"
test -f "$ROOT/opencode/opencode"
test -f "$ROOT/common/frontends.json"
mkdir -p "$ROOT/state" "$ROOT/data/workspaces/web-test"

# 服务按数字 UID/GID 10001 访问宿主机卷。
chown 10001:10001 "$ROOT"
chown -R 10001:10001 "$ROOT/source" "$ROOT/common" \
  "$ROOT/data" "$ROOT/state" "$ROOT/opencode"

# 所有目录都可进入；源码可读，配置和可写数据不开放给其他用户。
chmod 0755 "$ROOT"
chmod -R u+rX "$ROOT/source"
chmod -R u+rwX,go-rwx "$ROOT/common" "$ROOT/data" "$ROOT/state"
chmod 0755 "$ROOT/opencode"

# 普通文件的 X 不会补执行位，OpenCode 二进制必须显式设置 x。
chmod 0555 "$ROOT/opencode/opencode"

# setpriv 来自 util-linux，无需宿主机存在 UID 10001 的用户记录。
command -v setpriv >/dev/null
setpriv --reuid=10001 --regid=10001 --clear-groups \
  sh -eu -c '
    test -r "$1/source/opencode_plugin/dist/index.mjs"
    test -x "$1/source/opencode_plugin/node_modules"
    test -r "$1/common/frontends.json"
    test -w "$1/state" && test -x "$1/state"
    test -w "$1/data/workspaces/web-test" && test -x "$1/data/workspaces/web-test"
    test -x "$1/opencode/opencode"
  ' sh "$ROOT"
echo '目录、插件、配置、工作区和 OpenCode 执行权限检查通过'
BASH
```

命令遇到错误会立即停止。若提示没有 setpriv，安装发行版的 util-linux 工具包再运行。CU 会在 state 下自动创建 control，不需要提前创建。上述权限检查不验证 CPU 架构、动态库或 SELinux；可进一步运行 `sudo setpriv --reuid=10001 --regid=10001 --clear-groups /data/opencode_backend_k8s/opencode/opencode --version` 验证二进制能够启动。

若 CU 已因 `/data/control` 权限失败而退出，完成上面整段后重启：

```bash
kubectl -n opencode-sv rollout restart deployment/container-up
kubectl -n opencode-sv rollout status deployment/container-up --timeout=180s
```

source 挂载到 `/mnt/source`；common 挂载到 `/mnt/common`；用户目录在 `/mnt/env/workspaces/<frontend>/<user>`；状态卷挂载到 `/data`。配置中使用容器内路径，不填写宿主机路径。

## 4. 填写实际配置

```bash
sudoedit "$DEPLOY_ROOT/common/frontends.json"
sudoedit "$DEPLOY_ROOT/common/web-test/config/opencode-server.json"
sudoedit "$DEPLOY_ROOT/common/web-test/config/user-permissions.yaml"
```

必须核对：

| 文件 | 配置内容 |
| --- | --- |
| frontends.json | `frontend_id=web-test`；bucket 镜像；实际 namespace/PVC/node_selector 与清单一致 |
| frontends.json | 异步回调 `send_msg_url`；测试代理可填 `http://web-server:8000/outbound` |
| frontends.json | `attachment_storage` 的 MinIO endpoint、凭据、bucket 和下载地址 |
| opencode-server.json | 模型 `provider.options.baseURL/apiKey`、models 中的模型名和 `model=provider/模型名` |
| opencode-server.json | instructions、skills.paths 指向本 frontend 的 `/mnt/common/web-test/app/` |
| user-permissions.yaml | `sandbox.enabled`、`session.enabled` 保持 true；需要下载 URL 时允许 sandbox.network |

MinIO 的 `attachments` bucket 需预先存在。`public_base_url` 非空返回未签名链接，必须可直接下载；如果需要签名链接，将它设为空，并确保 endpoint 对最终下载方可达。仅在集群内可解析的 endpoint 不适合给集群外用户下载。

本文主流程假设已有 MinIO；自建资源参考 `k8s/minio/minio.yaml`，部署前按实际节点、存储和凭据调整，不直接套用其中的环境值。

从部署源码目录用容器检查实际配置，避免仅检查仓库里的示例：

```bash
docker run --rm --user 10001:10001 \
  -v "$DEPLOY_ROOT/source:/mnt/source:ro" \
  -v "$DEPLOY_ROOT/common:/mnt/common:ro" \
  -w /mnt/source agent-container-up:opencode-1.18.31 \
  python -m container_up --check-config --config /mnt/common/frontends.json
```

此命令适用于节点上也有 Docker 且已导入镜像的情况；只有 containerd 时可在部署后使用下一节的 `kubectl exec` 检查。schema 通过不代表模型、MinIO、文件权限和 bwrap 可用。

## 5. 部署 Kubernetes 资源

从仓库根目录执行。默认 `storage.yaml` 的 `storage_backend` 为 local。如果选择 NFS，须在**首次 PVC 绑定之前**修改 backend 选择及三个 NFS PV 的 server/export，state 仍保留本地盘；已绑定 PVC 不能通过改 selector 完成迁移。

```bash
kubectl label node ANQUANZHOUQI-168 opencode-runtime=true --overwrite
kubectl apply -f k8s/opencode/storage.yaml
kubectl -n opencode-sv get pvc
kubectl apply -f k8s/opencode/backend.yaml
kubectl -n opencode-sv rollout status deployment/container-up --timeout=180s
kubectl -n opencode-sv get pods,deployments,svc,pvc
kubectl -n opencode-sv logs deployment/container-up --tail=100
kubectl -n opencode-sv exec deployment/container-up -- \
  python -m container_up --check-config --config /mnt/common/frontends.json
curl -i -sS 'http://192.168.199.168:30082/health/live'
curl -i -sS 'http://192.168.199.168:30082/health/ready'
```
z
健康检查命令中的示例 IP 请替换为实际节点地址。预期 CU Running/Ready、四个 PVC Bound，健康接口返回 ok/ready。首次部署 `connected_buckets=[]`、没有 bucket Pod 是正常的。不要增加 CU 副本数，共享 control.sqlite 使用单写进程。

## 6. 同步消息与会话测试（直接 curl）

以下命令在 Bash 中直接执行，不需要 jq、环境变量或请求文件。将 `192.168.199.168:30082` 替换为实际 CU 节点地址；`web-test` 必须是已注册的 frontend。示例 IP 仅作占位，不表示已验证该服务器。

发送消息：

```bash
curl -i -sS --max-time 660 -X POST 'http://192.168.199.168:30082/inbound-sync/web-test' \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "deploy-smoke",
    "chat_id": "smoke-chat-001",
    "request_id": "smoke-request-001",
    "content": "请记住测试代号蓝鲸472，并只回复已记住。",
    "attachments": [],
    "metadata": {}
  }'

预期 HTTP 200，响应 `status` 为 `completed`，`content` 为模型回复。首次请求会自动建桶；HTTP 200 也可能带 failed/cancelled，需检查响应。同步请求不触发回调，delivery_state 为 pending 不代表执行失败。每次新测试换一个 request_id；原样重发同一 ID 只返回已有请求结果。

继续同一会话：

```bash
curl -i -sS --max-time 660 -X POST 'http://192.168.199.168:30082/inbound-sync/web-test' \
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

## 7. 查询、去重和冲突测试

查询第 6 节请求：

```bash
curl -i -sS 'http://192.168.199.168:30082/requests/web-test/smoke-request-001'
```

去重测试：原样再次执行第 6 节第一条 curl，预期返回相同请求结果，不重复执行。冲突测试：保留 ID、修改 content，预期 HTTP 409：

```bash
curl -i -sS -X POST 'http://192.168.199.168:30082/inbound-sync/web-test' \
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

## 8. 异步消息和回调测试

先配置 frontend 的 send_msg_url 为实际业务回调。使用示例 Web 代理接收时，填 `http://web-server:8000/outbound`，在无执行中任务时执行：

```bash
kubectl apply -f k8s/opencode/web-server.yaml
kubectl -n opencode-sv rollout status deployment/web-server --timeout=180s
kubectl -n opencode-sv rollout restart deployment/container-up
kubectl -n opencode-sv rollout status deployment/container-up --timeout=180s
```

异步发送：

```bash
curl -i -sS -X POST 'http://192.168.199.168:30082/inbound/web-test' \
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
curl -i -sS 'http://192.168.199.168:30082/requests/web-test/async-request-001'
```

预期最终 status=completed、delivery_state=sent。unknown 表示投递结果不确定，服务不会盲目重发。使用 Web 代理时查看回调日志：

```bash
kubectl -n opencode-sv logs deployment/web-server --tail=100
```

预期出现 `Outbound message received`。Web `/inbound` 是异步代理，同步请求直接访问 CU。

## 9. 取消测试

先提交一个耗时请求：

```bash
curl -i -sS -X POST 'http://192.168.199.168:30082/inbound/web-test' \
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
curl -i -sS -X POST 'http://192.168.199.168:30082/requests/web-test/cancel-request-001/cancel'
```

查询最终状态：

```bash
curl -i -sS 'http://192.168.199.168:30082/requests/web-test/cancel-request-001'
```

取消接口先返回当前状态，稍后查询应为 cancelled。如果已经完成，不覆盖终态。要验证执行中的进程终止，先从 bucket 日志确认 sleep 已启动再取消。

## 10. 文件生成、发布和附件测试

生成文件并发布到 MinIO：

```bash
curl -i -sS --max-time 660 -X POST 'http://192.168.199.168:30082/inbound-sync/web-test' \
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
curl -i -sS --max-time 660 -X POST 'http://192.168.199.168:30082/inbound-sync/web-test' \
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

## 11. 源码与配置更新

先停止业务提交并确认没有执行中任务。备份 common 和 state 应在服务停止写入时执行，运行中的 SQLite 不能简单复制单个 db 文件作为一致备份。不要删除 state 来更新代码。

在存储节点从新版本仓库根目录同步源码；插件有修改先在构建机执行 npm ci/build 并带上结果：

```bash
npm ci --prefix opencode_plugin
npm run build --prefix opencode_plugin
sudo rsync -a --exclude=.git/ --exclude=.venv/ --exclude=.opencode-reference/ \
  --exclude=vendor/ --exclude='*.tar' --exclude='*.tar.gz' ./ "$DEPLOY_ROOT/source/"
# 接着完整执行第 3 节的统一权限命令，再执行以下重启命令。
kubectl -n opencode-sv rollout restart deployment/container-up
kubectl -n opencode-sv rollout status deployment/container-up --timeout=180s
kubectl -n opencode-sv rollout restart deployment -l app=opencode-bucket
```

这一步不覆盖 common 中的业务配置；需要更新 skills/模板时单独同步相应 frontend 的 app。只改源码、插件或共享二进制无需重建基础镜像，Python/系统依赖变化则需要重建并换镜像标签。二进制使用第 3 节的 `.new` + rename 方式替换。

bash 直接包装 bwrap 的版本要求 runtime 与插件协议均为 6；须同步 Python 源码与重新构建的插件并重启 bucket。确认镜像内存在 `/usr/bin/flock`（util-linux），缺失时使用更新后的 Dockerfile 重建镜像。旧部署目录中的 `bucket_runtime/exec_client.py` 可删除，新链路不再引用它。业务 bash 输出限制改由 OpenCode 原生工具处理，runtime 的 `limits.output_bytes` 不再限制业务 bash。

修改 bucket 镜像、挂载或启动参数时，仅 rollout restart 不够。在安装了项目依赖且可运行 kubectl 的环境生成清单：

```bash
python3 -m scripts.render_bucket_updates \
  --config "$DEPLOY_ROOT/common/frontends.json" > /tmp/opencode-bucket-updates.yaml
kubectl diff -f /tmp/opencode-bucket-updates.yaml
# diff 返回 1 表示存在差异；检查内容后应用
kubectl apply -f /tmp/opencode-bucket-updates.yaml
kubectl apply -f k8s/opencode/backend.yaml
```

工具保留副本数并要求明确的 frontend annotation。新策略与已有 session 权限不一致会拒绝执行；权限升级需另行处理已有会话，不能假设重启会自动迁移或修正策略。旧 nanobot 状态也不通过该工具转换。

## 12. 排查命令

```bash
kubectl -n opencode-sv get pods -o wide
kubectl -n opencode-sv get events --sort-by=.metadata.creationTimestamp
kubectl -n opencode-sv describe pvc
kubectl -n opencode-sv logs deployment/container-up --tail=200
kubectl -n opencode-sv get pods -l app=opencode-bucket
# 将实际 Pod 名填入；runtime 容器 stdout 包含 OpenCode 日志和 runtime 输出的精简会话日志
export BUCKET_POD=REPLACE_WITH_BUCKET_POD
kubectl -n opencode-sv describe pod "$BUCKET_POD"
kubectl -n opencode-sv logs "$BUCKET_POD" --all-containers --tail=200
```

| 现象 | 检查方向 |
| --- | --- |
| PVC Pending / Pod Pending | PV selector、节点标签、目录是否存在、容量及调度事件 |
| ImagePullBackOff | 镜像是否导入节点实际运行时，或内网仓库标签与配置是否匹配 |
| CU Ready 但请求建桶失败 | bucket 日志、插件 dist/node_modules、二进制权限、bwrap 探测 |
| HTTP 404 frontend is not registered | 使用总表中实际 frontend_id；示例为 web-test |
| HTTP 422 | chat_id 缺失/为空、携带 raw/未知字段或字段类型不匹配 |
| HTTP 409 | request_id 已被不同输入或另一种同步/异步模式使用 |
| HTTP 504 | 按原 request_id 查询；检查模型、建桶和插件事件链路 |
| 附件有 URL 但下载失败 | MinIO bucket 权限、URL 可达性、签名有效期及 endpoint |
| 更新后 EXECUTION_INTERRUPTED | 未完成执行被进程重启打断，检查副作用后决定是否重新提交 |

## 13. 开发侧回归测试

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

验收至少完成：CU Ready、请求自动建桶、同步 completed、同会话上下文、去重/409、附件下载；使用异步业务时还需验证 delivery_state=sent 和业务回调日志。本文命令按仓库实现编写，实际模型、MinIO 和集群能力需在目标环境运行验证。

## 14. 删除命名空间、PVC 和 PV

以下命令用于卸载或重新部署，会停止 opencode-sv 中全部服务。只删除 Kubernetes 资源，不删除服务器目录，也不删除独立 MinIO 命名空间或其 PV。当前清单的 PV 均为 Retain，source/common/data/state 中的文件和数据库继续保留。

### 14.1 整套删除（包含命名空间和 PVC）

删除命名空间会自动删除其中的 Deployment、Pod、Service 和 PVC；PV 是集群级资源，须单独删除。等待命名空间删除完成后，再删除 PV：

```bash
kubectl delete namespace opencode-sv --ignore-not-found --wait=true --timeout=180s
```

确认上一步完成后执行。storage.yaml 同时定义 local 和 NFS PV，因此这里把本项目七个 PV 全部列出：

```bash
kubectl delete pv \
  opencode-data-pv-local \
  opencode-source-pv-local \
  opencode-common-pv-local \
  opencode-state-pv-local \
  opencode-data-pv-nfs \
  opencode-source-pv-nfs \
  opencode-common-pv-nfs \
  --ignore-not-found --wait=true --timeout=180s
```

### 14.2 保留命名空间，只删除 PVC 和 PV

这是上面整套删除的替代方案。先停止 CU，避免它继续创建 bucket，再删除全部工作负载，等待 Pod 退出后删除 PVC：

```bash
kubectl -n opencode-sv scale deployment/container-up --replicas=0
kubectl -n opencode-sv delete deployment --all --wait=true
kubectl -n opencode-sv wait --for=delete pod --all --timeout=180s
kubectl -n opencode-sv delete pvc \
  opencode-data-pvc \
  opencode-source-pvc \
  opencode-common-pvc \
  opencode-state-pvc \
  --ignore-not-found --wait=true --timeout=180s
```

确认 PVC 已删除后，执行 14.1 中的七个 PV 删除命令。此方式保留命名空间、Service、RBAC 等资源。

### 14.3 检查及重新部署

```bash
kubectl get namespace opencode-sv
kubectl get pv
```

整套删除后，第一条返回 NotFound 属正常情况；第二条确认上述 opencode PV 已消失。若删除超时，先检查残留 Pod/PVC，不直接强删 finalizer：

```bash
kubectl -n opencode-sv get pods,pvc
kubectl -n opencode-sv get events --sort-by=.metadata.creationTimestamp
```

重新部署：

```bash
kubectl apply -f k8s/opencode/storage.yaml
kubectl -n opencode-sv get pvc
kubectl apply -f k8s/opencode/backend.yaml
kubectl -n opencode-sv rollout status deployment/container-up --timeout=180s
```

保留的 state 数据库仍记录旧请求和会话，因此上述操作不是清空业务数据。仅删除 PVC 而保留旧 PV 时，Retain PV 可能处于 Released 并保留旧 claimRef；重新部署前按上述步骤删除并重建本项目 PV 对象，不要误删其他项目的 PV。
