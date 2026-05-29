# Nanobot K8s 部署手册

本文只针对当前仓库的 Kubernetes 动态分桶部署方案，覆盖：

- 依赖安装
- 共享存储准备
- 镜像构建
- Kind 本地验证
- 生产 K8s 集群部署
- 验证与运维命令

当前部署架构对应两个核心服务：

- `container_up`
  - 统一网关，负责接收入站请求、动态创建 bucket、统一出站。
- `bucket_runtime`
  - bucket Pod 内运行时，负责启动和管理用户级 Nanobot 进程。

## 1. 部署前提

### 1.1 操作系统建议

推荐：

- Ubuntu 22.04 / 24.04
- Docker 可用
- Kubernetes 集群网络互通

### 1.2 必装依赖

K8s 部署至少需要：

- `git`
- `docker`
- `kubectl`
- `kind`
  - 只在本地验证时需要
- `nfs-kernel-server`
  - 只在你使用 NFS 服务端时需要
- `nfs-common`
  - K8s 节点使用 NFS 挂载时需要

### 1.3 项目内实际依赖

从当前仓库看，K8s 部署关键依赖来自：

- Python 包：
  - `fastapi`
  - `uvicorn`
  - `httpx`
  - `websockets`
  - `pydantic`
  - `openai`
  - `anthropic`
- 系统工具：
  - `kubectl`

说明：

- `Dockerfile.bucket_runtime` 已安装 Python 运行依赖
- `Dockerfile.container_up` 已安装 Python 运行依赖
- 当前项目的 bucket 动态创建逻辑在 [container_up/bucket_manager.py](../container_up/bucket_manager.py) 中通过 `kubectl apply` 和 `kubectl scale` 实现，因此 `container_up` 镜像必须包含 `kubectl`

## 2. 依赖安装命令

以下命令默认适用于 Ubuntu。

### 2.1 安装 Git 和基础工具

```bash
sudo apt update
sudo apt install -y git curl ca-certificates gnupg
```

### 2.2 安装 Docker 和 Docker Compose

```bash
sudo apt update
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
docker --version
docker compose version
```

如需免 sudo：

```bash
sudo usermod -aG docker $USER
newgrp docker
```

### 2.3 安装 kubectl

AMD64:

```bash
curl -LO "https://dl.k8s.io/release/v1.33.1/bin/linux/amd64/kubectl"
sudo install -m 0755 kubectl /usr/local/bin/kubectl
rm -f kubectl
kubectl version --client
```

ARM64:

```bash
curl -LO "https://dl.k8s.io/release/v1.33.1/bin/linux/arm64/kubectl"
sudo install -m 0755 kubectl /usr/local/bin/kubectl
rm -f kubectl
kubectl version --client
```

### 2.4 安装 kind

AMD64:

```bash
curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.29.0/kind-linux-amd64
chmod +x ./kind
sudo mv ./kind /usr/local/bin/kind
kind version
```

ARM64:

```bash
curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.29.0/kind-linux-arm64
chmod +x ./kind
sudo mv ./kind /usr/local/bin/kind
kind version
```

### 2.5 安装 NFS

NFS 服务端：

```bash
sudo apt update
sudo apt install -y nfs-kernel-server
sudo systemctl enable --now nfs-kernel-server
```

K8s 节点：

```bash
sudo apt update
sudo apt install -y nfs-common
sudo systemctl enable --now rpcbind
```

## 3. 拉取源码

```bash
git clone https://github.com/HKUDS/nanobot.git
cd nanobot
```

如果你部署的是自己的分支，请替换仓库地址。

## 4. K8s 目录和挂载约定

当前仓库的 K8s 方案只依赖一个共享挂载根：

- `BUCKET_MOUNT_ROOT=/mnt/nanobot`
- `SOURCE_ROOT=/workspace/nanobot`

数据根和源码根的职责分开：

- `BUCKET_MOUNT_ROOT`
  - 挂共享数据目录
- `SOURCE_ROOT`
  - 挂共享源码目录

在当前实现里，`SOURCE_ROOT` 下固定推导三段源码路径：

- `SOURCE_ROOT/container_up`
- `SOURCE_ROOT/bucket_runtime`
- `SOURCE_ROOT/nanobot`

`BUCKET_MOUNT_ROOT` 下固定按以下结构读取：

- `routedb/`
  - `container_up` 的运行态数据库目录
- `source/`
  - bucket Pod 的工作源码目录
- `common/frontends.json`
  - frontend 注册表
- `common/<frontend_id>/config.json`
  - 对应 frontend 的 Nanobot 配置
- `common/<frontend_id>/skills/`
  - 对应 frontend 的公共 skills
- `common/<frontend_id>/templates/`
  - 对应 frontend 的模板目录
- `workspaces/<frontend_id>/<user_id>/`
  - 用户工作目录

当前实现里这些路径都是固定推导的，不再单独配置 `skills`、`templates`、`frontends`、`workspaces` 的挂载路径，也不再分别配置三段源码路径。

## 5. 配置准备

### 5.1 frontend 注册表

当前网关和 bucket runtime 都会读取：

- [host_test_env/common/frontends.json](../host_test_env/common/frontends.json)

在 K8s 共享目录中要把它放到：

```text
common/frontends.json
```

### 5.2 每个 frontend 的公共目录

每个 frontend 的公共目录是固定路径，不再在 `frontends.json` 里配置 `common_root`。目录结构如下：

```text
common/
  frontends.json
  feishu-main/
    config.json
    skills/
    templates/
  feishu-sub/
    config.json
    skills/
    templates/
  qxt-main/
    config.json
    skills/
    templates/
```

### 5.3 必须检查的配置项

部署前至少确认：

1. `common/frontends.json` 里的 `id`、`provider` 正确
2. 每个 `common/<frontend-id>/config.json` 中模型配置和 API Key 正确
3. `common/frontends.json` 中不要保留示例密钥
4. `common/<frontend-id>/templates/` 已准备好模板

## 6. 方式一：Kind 本地 K8s 验证

这个流程适合先在本机验证整套 K8s 方案。

### 6.1 准备 Kind 挂载目录

当前仓库的 dev-kind 配置直接使用：

- 当前工作区作为 `SOURCE_ROOT`
- `host_test_env/` 作为 `BUCKET_MOUNT_ROOT` 的底层数据目录

也就是说，Kind 节点里会看到：

- `/workspace/nanobot`
  - 对应当前仓库根目录
- `/data/nanobot-kind/host_test_env`
  - 对应当前仓库下的 `host_test_env/`

因此本地验证前只需要确保 `host_test_env/` 下至少有这些内容：

```text
host_test_env/
  common/
    frontends.json
    <frontend_id>/
      config.json
      skills/
      templates/
  routedb/
  workspaces/
```

如果你要替换某个 frontend 的真实配置，直接覆盖：

```bash
cp /path/to/your/feishu-main-config.json ./host_test_env/common/feishu-main/config.json
cp /path/to/your/feishu-sub-config.json ./host_test_env/common/feishu-sub/config.json
cp /path/to/your/qxt-main-config.json ./host_test_env/common/qxt-main/config.json
```

### 6.2 构建镜像

构建 bucket runtime：

```bash
docker build -f Dockerfile.bucket_runtime -t nanobot-bucket-runtime:v1.0.0 .
```

构建 container_up：

```bash
docker build -f Dockerfile.container_up -t nanobot-container-up:v1.0.0 .
```

### 6.3 创建 Kind 集群

```bash
kind create cluster --config k8s/dev-kind/kind-cluster.yaml
kubectl cluster-info --context kind-nanobot
```

### 6.4 导入镜像

```bash
kind load docker-image nanobot-bucket-runtime:v1.0.0 --name nanobot
kind load docker-image nanobot-container-up:v1.0.0 --name nanobot
```

### 6.5 部署资源

```bash
kubectl apply -f k8s/base/namespace.yaml
kubectl apply -f k8s/base/rbac.yaml
kubectl apply -f k8s/dev-kind/storage-local.yaml
kubectl apply -f k8s/base/container-up.yaml
```

### 6.6 查看部署状态

```bash
kubectl get pods -n nanobot
kubectl get svc -n nanobot
kubectl get pvc -n nanobot
kubectl logs -n nanobot deploy/container-up -f
```

### 6.7 暴露网关并做健康检查

```bash
kubectl port-forward -n nanobot svc/container-up 8080:8080
```

新开一个终端：

```bash
curl http://127.0.0.1:8080/health/live
curl http://127.0.0.1:8080/health/ready
curl http://127.0.0.1:8080/healthz
```

### 6.8 验证 bucket 动态创建

注意：

- `container_up` 在创建 bucket 之前，会先在 `workspaces/<frontend_id>/<user_id>/` 下创建用户 workspace
- 该目录在网关 Pod 内必须是可写的
- 触发入口是 `POST /inbound/{frontend_id}`，不是 `POST /inbound`

```bash
curl --noproxy '*' -X POST http://127.0.0.1:8080/inbound/feishu-main \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "demo-user",
    "chat_id": "default",
    "content": "hello",
    "attachments": [],
    "metadata": {},
    "raw": {}
  }'
```

然后查看 bucket 是否创建：

```bash
kubectl get deploy,po,svc -n nanobot
kubectl logs -n nanobot deploy/container-up --tail=200
kubectl logs -n nanobot deploy/nanobot-bucket-0 --tail=200
```

## 7. 方式二：生产 K8s 集群部署

如果是正式集群，重点在于共享存储和镜像仓库。

### 7.1 准备 NFS 服务端目录

```bash
sudo mkdir -p /data/nanobot-nfs/routedb
sudo mkdir -p /data/nanobot-nfs/source
sudo mkdir -p /data/nanobot-nfs/common
sudo mkdir -p /data/nanobot-nfs/workspaces
sudo chown -R 1000:1000 /data/nanobot-nfs
sudo chmod -R 775 /data/nanobot-nfs
```

配置导出：

```bash
cat <<'EOF' | sudo tee /etc/exports
/data/nanobot-nfs *(rw,sync,no_subtree_check,no_root_squash)
EOF
sudo exportfs -ra
sudo systemctl restart nfs-kernel-server
sudo exportfs -v
```

### 7.2 同步共享内容到 NFS

```bash
sudo rsync -av --delete ./ /data/nanobot-nfs/source/
sudo cp ./host_test_env/common/frontends.json /data/nanobot-nfs/common/frontends.json
```

准备 frontend 公共目录：

```bash
sudo mkdir -p /data/nanobot-nfs/common/feishu-main/skills
sudo mkdir -p /data/nanobot-nfs/common/feishu-main/templates
sudo cp /path/to/your/feishu-main-config.json /data/nanobot-nfs/common/feishu-main/config.json
sudo rsync -av ./nanobot/templates/ /data/nanobot-nfs/common/feishu-main/templates/
```

其他 frontend 同理：

```bash
sudo mkdir -p /data/nanobot-nfs/common/feishu-sub/skills
sudo mkdir -p /data/nanobot-nfs/common/feishu-sub/templates
sudo cp /path/to/your/feishu-sub-config.json /data/nanobot-nfs/common/feishu-sub/config.json
sudo rsync -av ./nanobot/templates/ /data/nanobot-nfs/common/feishu-sub/templates/

sudo mkdir -p /data/nanobot-nfs/common/qxt-main/skills
sudo mkdir -p /data/nanobot-nfs/common/qxt-main/templates
sudo cp /path/to/your/qxt-main-config.json /data/nanobot-nfs/common/qxt-main/config.json
sudo rsync -av ./nanobot/templates/ /data/nanobot-nfs/common/qxt-main/templates/
```

### 7.3 构建并推送镜像

构建镜像：

```bash
docker build -f Dockerfile.bucket_runtime -t nanobot-bucket-runtime:v1.0.2 .
docker build -f Dockerfile.container_up -t nanobot-container-up:v1.0.1 .
```

推送到私有仓库：

```bash
docker tag nanobot-bucket-runtime:v1.0.0 <your-registry>/nanobot-bucket-runtime:v1.0.0
docker tag nanobot-container-up:v1.0.0 <your-registry>/nanobot-container-up:v1.0.0
docker push <your-registry>/nanobot-bucket-runtime:v1.0.0
docker push <your-registry>/nanobot-container-up:v1.0.0
```

### 7.4 修改 K8s YAML

部署前需要编辑三个文件：

- [k8s/base/container-up.yaml](../k8s/base/container-up.yaml)
- [k8s/base/nanobot-bucket-template.yaml](../k8s/base/nanobot-bucket-template.yaml)
- [k8s/base/pv-pvc-nfs.yaml](../k8s/base/pv-pvc-nfs.yaml) 或你自己的存储清单

至少改这几项：

1. `image`
2. `imagePullPolicy`
3. 共享 PVC 名称必须与 `container-up.yaml` 中的 `BUCKET_MOUNT_PVC` 一致
4. NFS 版本下的 PV `server`
5. NFS 版本下的 PV `path`

当前最新实现要求：

- 只提供一个共享 PVC，例如 `nanobot-data-pvc`
- 该 PVC 在 Pod 内统一挂载到 `/mnt/nanobot`
- PVC 内部目录结构必须包含：
  - `routedb/`
  - `source/`
  - `common/`
  - `workspaces/`

如果你直接使用仓库里的 `k8s/base/pv-pvc-nfs.yaml` 或 `k8s/dev-kind/storage-local.yaml`，需要先把它们从旧的多 PV/PVC 结构改成单 PV/PVC 结构后再应用。

如果使用远程镜像仓库，推荐：

- `imagePullPolicy: IfNotPresent`

### 7.5 应用资源

创建命名空间和 RBAC：

```bash
kubectl apply -f k8s/base/namespace.yaml
kubectl apply -f k8s/base/rbac.yaml
kubectl apply -f k8s/base/pv-pvc-nfs.yaml
kubectl apply -f k8s/base/container-up.yaml
kubectl apply -f k8s/base/container-up-nodeport.yaml
kubectl rollout status deployment/container-up -n nanobot
kubectl get pods -n nanobot -o wide
```
kubectl apply -f k8s/base/pv-pvc-hostpath.yaml
kubectl delete ns nanobot && kubectl delete pv nanobot-data-pv nanobot-source-pv nanobot-common-pv
查看日志：

```bash
kubectl logs -n nanobot deploy/container-up -f
kubectl logs -n nanobot deploy/nanobot-bucket-0 -f
```

web server测试：
说明：`attachments` 里如果已经是 `http/https` URL，直接按 URL 使用；不要下载到本地，也不要写入本地缓存。仅当附件是本地路径时，才走上传并替换为可访问 URL。

env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy curl -sS -X POST http://127.0.0.1:8090/inbound -H 'Content-Type: application/json' -d '{"frontend_id":"web-wd","user_id":"web-demo-2","chat_id":"web-chat-2","content":"给我生成一个txt，里面写sucess，作为附件发给我","attachments":[],"metadata":{},"raw":{}}'

curl -sS -X POST 'http://127.0.0.1:8090/inbound' \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "frontend_id": "web-wd",
  "user_id": "web-demo-2",
  "chat_id": "web-chat-2",
  "content": "/new",
  "attachments": [],
  "metadata": {},
  "raw": {}
}
JSON

curl -sS -X POST 'http://127.0.0.1:8090/inbound' \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "frontend_id": "web-wd",
  "user_id": "web-demo-2",
  "chat_id": "web-chat-2",
  "content": "/cancel",
  "attachments": [],
  "metadata": {},
  "raw": {}
}
JSON

curl -sS -X POST 'http://127.0.0.1:8090/inbound' \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "frontend_id": "web-main",
  "user_id": "web-demo-2",
  "chat_id": "web-chat-2",
  "content": "创建一个txt文件",
  "attachments": [],
  "metadata": {},
  "raw": {}
}
JSON

curl -sS -X POST 'http://127.0.0.1:8090/inbound' \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "frontend_id": "web-wd",
  "user_id": "web-demo-2",
  "chat_id": "web-chat-2",
  "content": "提交人孙宸，提交时间2026/05/20，分析图片中的安全风险",
  "attachments": [
    "http://192.168.48.104:9000/attachments/2026/05/19/3eeed76636d54c9c9e0252c6517e5cd0/tmp_c8c38602127542f09ea8336e65661a10.jpg","http://192.168.48.104:9000/attachments/2026/05/20/a835cde7fcf943ea9922023770ab0242/tmp_f79d82c30e97bfc19447817a3adc76b3.jpg"
  ],
  "metadata": {},
  "raw": {}
}
JSON


curl -sS -X POST 'http://127.0.0.1:8090/inbound' \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "frontend_id": "web-wd",
  "user_id": "web-demo-2",
  "chat_id": "web-chat-2",
  "content": "提交人孙宸，提交时间2026/05/20，分析视频中的安全风险",
  "attachments": [
    "http://192.168.48.104:9000/attachments/2026/05/28/4b134cb715764ba7b64f63063d67e7e1/1.mp4"
  ],
  "metadata": {},
  "raw": {}
}
JSON

`POST /test/create-instances` 用于一次性创建多条测试会话并并发转发到 `container-up`，适合压测或批量验证实例拉起逻辑。接口会自动生成 `user_id` 和 `chat_id`，格式分别为 `test-user-{batch_id}-{index}`、`test-chat-{batch_id}-{index}`。

请求示例：

```bash
curl -sS -X POST 'http://127.0.0.1:8090/test/create-instances' \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "frontend_id": "web-stream",
  "n": 3,
  "content": "测试批量拉起实例"
}
JSON
```

参数说明：

- `frontend_id`：目标前端 ID，对应实际转发的 `/inbound/{frontend_id}`。
- `n`：创建的测试实例数量，取值范围 `1-1000`。
- `content`：发送给每个测试实例的消息内容，默认值为 `ping`。

返回示例：

```json
{
  "status": "accepted",
  "frontend_id": "web-stream",
  "batch_id": "a1b2c3d4",
  "count": 3,
  "success_count": 3,
  "failure_count": 0,
  "requests": [
    {
      "user_id": "test-user-a1b2c3d4-1",
      "chat_id": "test-chat-a1b2c3d4-1"
    },
    {
      "user_id": "test-user-a1b2c3d4-2",
      "chat_id": "test-chat-a1b2c3d4-2"
    },
    {
      "user_id": "test-user-a1b2c3d4-3",
      "chat_id": "test-chat-a1b2c3d4-3"
    }
  ],
  "responses": [
    {
      "user_id": "test-user-a1b2c3d4-1",
      "chat_id": "test-chat-a1b2c3d4-1",
      "status": "accepted",
      "response": {
        "status": "accepted"
      }
    },
    {
      "user_id": "test-user-a1b2c3d4-2",
      "chat_id": "test-chat-a1b2c3d4-2",
      "status": "accepted",
      "response": {
        "status": "accepted"
      }
    },
    {
      "user_id": "test-user-a1b2c3d4-3",
      "chat_id": "test-chat-a1b2c3d4-3",
      "status": "accepted",
      "response": {
        "status": "accepted"
      }
    }
  ]
}
```

如果批量创建过程中部分实例失败，接口不会再整批直接报错，而是返回 `status: "partial_success"`，并在 `responses` 中标出失败项，例如：

```json
{
  "status": "partial_success",
  "frontend_id": "web-stream",
  "batch_id": "a1b2c3d4",
  "count": 3,
  "success_count": 2,
  "failure_count": 1,
  "responses": [
    {
      "user_id": "test-user-a1b2c3d4-1",
      "chat_id": "test-chat-a1b2c3d4-1",
      "status": "accepted",
      "response": {
        "status": "accepted"
      }
    },
    {
      "user_id": "test-user-a1b2c3d4-2",
      "chat_id": "test-chat-a1b2c3d4-2",
      "status": "failed",
      "error": "{\"detail\":\"Server error '503 Service Unavailable' for url 'http://nanobot-bucket-1.nanobot:8080/instances'\"}"
    },
    {
      "user_id": "test-user-a1b2c3d4-3",
      "chat_id": "test-chat-a1b2c3d4-3",
      "status": "accepted",
      "response": {
        "status": "accepted"
      }
    }
  ]
}
```
