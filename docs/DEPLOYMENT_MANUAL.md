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

当前仓库的 K8s 方案依赖以下共享目录：

- `route-db/`
  - `container_up` 的运行态数据库
- `source/`
  - 代码目录，只读挂载
- `common/`
  - 每个 frontend 的公共配置、skills、templates，只读挂载
- `frontends/`
  - 全局 `frontends.json`，只读挂载
- `workspaces/`
  - 用户工作目录，读写挂载

对应 K8s YAML 中的约定路径：

- `/mnt/nanobot/route-db`
- `/mnt/nanobot/source`
- `/mnt/nanobot/common`
- `/mnt/nanobot/frontends`
- `/mnt/nanobot/workspaces`

## 5. 配置准备

### 5.1 frontend 注册表

当前网关和 bucket runtime 都会读取：

- [workspace/frontends.json](../workspace/frontends.json)

在 K8s 中要把它放到：

```text
frontends/frontends.json
```

### 5.2 每个 frontend 的公共目录

每个 frontend 需要一个 `common_root`，目录结构如下：

```text
common/
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

1. `frontends.json` 里的 `id`、`provider`、`common_root` 正确
2. 每个 `common/<frontend-id>/config.json` 中模型配置和 API Key 正确
3. `workspace/frontends.json` 中不要保留示例密钥
4. `common/<frontend-id>/templates/` 已准备好模板

## 6. 方式一：Kind 本地 K8s 验证

这个流程适合先在本机验证整套 K8s 方案。

### 6.1 准备 Kind 挂载目录

```bash
mkdir -p /tmp/nanobot-kind/route-db
mkdir -p /tmp/nanobot-kind/source
mkdir -p /tmp/nanobot-kind/common
mkdir -p /tmp/nanobot-kind/frontends
mkdir -p /tmp/nanobot-kind/workspaces
```

同步源码和 frontend 配置：

```bash
rsync -av --delete ./ /tmp/nanobot-kind/source/
cp ./workspace/frontends.json /tmp/nanobot-kind/frontends/frontends.json
```

准备 frontend 公共目录。下面以 `feishu-main` 为例：

```bash
mkdir -p /tmp/nanobot-kind/common/feishu-main/skills
mkdir -p /tmp/nanobot-kind/common/feishu-main/templates
cp /path/to/your/feishu-main-config.json /tmp/nanobot-kind/common/feishu-main/config.json
rsync -av ./nanobot/templates/ /tmp/nanobot-kind/common/feishu-main/templates/
```

如果还有其他 frontend，逐个准备：

```bash
mkdir -p /tmp/nanobot-kind/common/feishu-sub/skills
mkdir -p /tmp/nanobot-kind/common/feishu-sub/templates
cp /path/to/your/feishu-sub-config.json /tmp/nanobot-kind/common/feishu-sub/config.json
rsync -av ./nanobot/templates/ /tmp/nanobot-kind/common/feishu-sub/templates/

mkdir -p /tmp/nanobot-kind/common/qxt-main/skills
mkdir -p /tmp/nanobot-kind/common/qxt-main/templates
cp /path/to/your/qxt-main-config.json /tmp/nanobot-kind/common/qxt-main/config.json
rsync -av ./nanobot/templates/ /tmp/nanobot-kind/common/qxt-main/templates/
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

```bash
curl -X POST http://127.0.0.1:8080/inbound \
  -H 'Content-Type: application/json' \
  -d '{
    "frontend_id": "feishu-main",
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
sudo mkdir -p /data/nanobot-nfs/route-db
sudo mkdir -p /data/nanobot-nfs/source
sudo mkdir -p /data/nanobot-nfs/common
sudo mkdir -p /data/nanobot-nfs/frontends
sudo mkdir -p /data/nanobot-nfs/workspaces
sudo chown -R 1000:1000 /data/nanobot-nfs
sudo chmod -R 775 /data/nanobot-nfs
```

配置导出：

```bash
cat <<'EOF' | sudo tee /etc/exports
/data/nanobot-nfs/route-db   *(rw,sync,no_subtree_check,no_root_squash)
/data/nanobot-nfs/source     *(ro,sync,no_subtree_check,no_root_squash)
/data/nanobot-nfs/common     *(ro,sync,no_subtree_check,no_root_squash)
/data/nanobot-nfs/frontends  *(ro,sync,no_subtree_check,no_root_squash)
/data/nanobot-nfs/workspaces *(rw,sync,no_subtree_check,no_root_squash)
EOF
sudo exportfs -ra
sudo systemctl restart nfs-kernel-server
sudo exportfs -v
```

### 7.2 同步共享内容到 NFS

```bash
sudo rsync -av --delete ./ /data/nanobot-nfs/source/
sudo cp ./workspace/frontends.json /data/nanobot-nfs/frontends/frontends.json
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
docker build -f Dockerfile.bucket_runtime -t nanobot-bucket-runtime:v1.0.0 .
docker build -f Dockerfile.container_up -t nanobot-container-up:v1.0.0 .
```

推送到私有仓库：

```bash
docker tag nanobot-bucket-runtime:v1.0.0 <your-registry>/nanobot-bucket-runtime:v1.0.0
docker tag nanobot-container-up:v1.0.0 <your-registry>/nanobot-container-up:v1.0.0
docker push <your-registry>/nanobot-bucket-runtime:v1.0.0
docker push <your-registry>/nanobot-container-up:v1.0.0
```

### 7.4 修改 K8s YAML

部署前需要编辑两个文件：

- [k8s/base/container-up.yaml](../k8s/base/container-up.yaml)
- [k8s/base/nanobot-bucket-template.yaml](../k8s/base/nanobot-bucket-template.yaml)

至少改这几项：

1. `image`
2. `imagePullPolicy`
3. NFS 版本下的 PV `server`
4. NFS 版本下的 PV `path`

如果使用远程镜像仓库，推荐：

- `imagePullPolicy: IfNotPresent`

### 7.5 应用资源

创建命名空间和 RBAC：

```bash
kubectl apply -f k8s/base/namespace.yaml
kubectl apply -f k8s/base/rbac.yaml
```

创建 NFS PV/PVC：

```bash
kubectl apply -f k8s/base/pv-pvc-nfs.yaml
kubectl get pv
kubectl get pvc -n nanobot
```

部署统一网关：

```bash
kubectl apply -f k8s/base/container-up.yaml
kubectl rollout status deployment/container-up -n nanobot
kubectl get pods -n nanobot -o wide
```

查看日志：

```bash
kubectl logs -n nanobot deploy/container-up -f
```

### 7.6 验证动态调度

先做端口转发：

```bash
kubectl port-forward -n nanobot svc/container-up 8080:8080
```

新开一个终端做健康检查：

```bash
curl http://127.0.0.1:8080/health/live
curl http://127.0.0.1:8080/health/ready
curl http://127.0.0.1:8080/healthz
```

再发送一条入站请求触发 bucket 创建：

```bash
curl -X POST http://127.0.0.1:8080/inbound \
  -H 'Content-Type: application/json' \
  -d '{
    "frontend_id": "feishu-main",
    "user_id": "demo-user",
    "chat_id": "default",
    "content": "hello",
    "attachments": [],
    "metadata": {},
    "raw": {}
  }'
```

检查资源变化：

```bash
kubectl get deploy,po,svc -n nanobot
kubectl logs -n nanobot deploy/container-up --tail=200
kubectl logs -n nanobot deploy/nanobot-bucket-0 --tail=200
```

## 8. 常用运维命令

查看资源：

```bash
kubectl get all -n nanobot
kubectl get pvc -n nanobot
kubectl describe pod -n nanobot <pod-name>
```

查看网关日志：

```bash
kubectl logs -n nanobot deploy/container-up --tail=200
```

查看 bucket 日志：

```bash
kubectl logs -n nanobot deploy/nanobot-bucket-0 --tail=200
```

重启网关：

```bash
kubectl rollout restart deployment/container-up -n nanobot
```

删除某个 bucket：

```bash
kubectl delete deployment -n nanobot nanobot-bucket-0
kubectl delete service -n nanobot nanobot-bucket-0
```

卸载：

```bash
kubectl delete -f k8s/base/container-up.yaml
kubectl delete -f k8s/base/rbac.yaml
kubectl delete -f k8s/base/pv-pvc-nfs.yaml
kubectl delete -f k8s/base/namespace.yaml
```

## 9. 部署检查清单

至少检查以下项：

1. `container-up` Pod Ready
2. `container-up` 容器内可执行 `kubectl`
3. `frontends/frontends.json` 已成功挂载
4. `common/<frontend-id>/config.json` 已成功挂载
5. `workspaces` PVC 可写
6. 首次入站请求可以自动创建 `nanobot-bucket-*`
7. `bucket_runtime` 能启动用户进程
8. 出站消息能回调到 `container_up`

## 10. 关键文件

部署时主要参考：

- [docs/PROJECT_ARCHITECTURE.md](./PROJECT_ARCHITECTURE.md)
- [docs/K8S_BRANCH_LAYOUT.md](./K8S_BRANCH_LAYOUT.md)
- [Dockerfile.container_up](../Dockerfile.container_up)
- [Dockerfile.bucket_runtime](../Dockerfile.bucket_runtime)
- [k8s/base/container-up.yaml](../k8s/base/container-up.yaml)
- [k8s/base/nanobot-bucket-template.yaml](../k8s/base/nanobot-bucket-template.yaml)
- [k8s/base/pv-pvc-nfs.yaml](../k8s/base/pv-pvc-nfs.yaml)
- [k8s/dev-kind/kind-cluster.yaml](../k8s/dev-kind/kind-cluster.yaml)
- [k8s/dev-kind/storage-local.yaml](../k8s/dev-kind/storage-local.yaml)

## 11. 注意事项

1. 当前仓库中的示例配置含有占位或历史配置值，正式部署前必须替换成你自己的配置。
2. 这套架构依赖共享存储，`source`、`common`、`frontends`、`workspaces` 缺一不可。
3. `container_up` 不只是网关，它还负责动态创建 bucket，因此镜像里必须带 `kubectl`。
4. 如果镜像从远程仓库拉取，必须同步调整 YAML 中的 `image` 和 `imagePullPolicy`。
5. `workspaces` 是持久目录，删除 bucket Pod 不会删除用户工作数据。
