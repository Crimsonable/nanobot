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

### 5.4 QXT 出站 TLS 证书检查

如果 `container_up` 的日志里出现类似：

```text
access_token retry attempt=1 error=Cannot connect to host ... ssl:True [SSLCertVerificationError: ... certificate has expired]
```

这通常不是重试能解决的问题，而是 `ACCESS_URL` 对应的上游 HTTPS 证书有问题。

优先检查：

1. 上游服务证书是否已经过期
2. 容器所在节点的系统时间是否正确
3. 是否走了企业内部代理或自签 CA，且容器里没有安装对应根证书

建议验证命令：

```bash
openssl s_client -connect im2test.chidi.com.cn:8888 -servername im2test.chidi.com.cn
```

处理原则：

- 如果证书确实过期，先在上游服务端更新证书
- 如果是内部 CA 信任链问题，把 CA 证书挂进容器并更新系统信任库
- 不要把 TLS 校验关闭作为长期方案

### 5.5 固定 MinIO 到指定节点

MinIO 使用的是 `hostPath` 存储，Pod 如果被调度到别的节点，会读到另一台机器上的目录。
如果你要让它始终固定在同一个节点上，需要先给目标节点打标签，再让 Deployment 只调度到该节点。

示例：

```bash
kubectl label node <node-name> nanobot.io/minio-node=true
```

然后在 `k8s/minio/minio.yaml` 的 Pod 模板里使用：

```yaml
spec:
  nodeSelector:
    nanobot.io/minio-node: "true"
```

如果是 Kind 的单节点环境，通常把 `control-plane` 节点打上这个标签即可。

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
kubectl apply -f k8s/base/base.yaml
kubectl apply -f k8s/base/engineering-scene-mcp.yaml
```

`base.yaml` 默认使用 `local` 存储；修改文件顶部的 `nanobot.io/storage-backend` 锚点为
`nfs` 即可切换到 NFS PV。该文件使用 `nanobot_node` 节点标签，并通过 `NANOBOT_NODE` 把同一个标签值传给
container-up。container-up 动态创建 bucket runtime Deployment 时，会自动加入相同的
`nodeSelector`。部署前先给目标节点打标签（正式环境默认值为 `prod`）：

```bash
kubectl label node <node-name> nanobot_node=prod
kubectl label node <mcp-node-name> nanobot_mcp_node=prod
```

如需使用其他标签值，只需修改 `base.yaml` 中锚点所在的一处：

```yaml
nodeSelector:
  nanobot_node: &nanobot_node "your-node-group"
```

若不需要节点限制，请同时删除清单中的 `nodeSelector` 和 `NANOBOT_NODE` 环境变量；代码在
`NANOBOT_NODE` 为空或未设置时不会给 bucket runtime 添加调度限制。

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

部署前需要编辑两个文件：

- [k8s/base/base.yaml](../k8s/base/base.yaml)
- [k8s/base/engineering-scene-mcp.yaml](../k8s/base/engineering-scene-mcp.yaml)

至少改这几项：

1. `image`
2. `imagePullPolicy`
3. 文件顶部的存储开关（`local` 或 `nfs`）
4. NFS PV 的 `server` 和 `path`（使用 NFS 时）
5. 正式环境和 MCP 的 `nodeSelector`

`base.yaml` 同时声明 local 和 NFS 两组候选 PV；三个 PVC 通过标签 selector 只绑定文件顶部选中的后端。
切换已绑定的 PVC 前，应先按 Kubernetes 存储迁移流程处理旧 PVC/PV 和数据，不能仅修改开关后直接覆盖。

如果使用远程镜像仓库，推荐：

- `imagePullPolicy: IfNotPresent`

### 7.5 应用资源

应用正式环境基础资源和独立的 MCP 配置：

```bash
kubectl apply -f k8s/base/base.yaml
kubectl apply -f k8s/base/engineering-scene-mcp.yaml
kubectl apply -f k8s/minio/minio.yaml

kubectl rollout status deployment/container-up -n nanobot
kubectl get pods -n nanobot -o wide
```

测试环境使用独立的 `nanobot-test` Namespace 和 `nanobot_node=test`。Namespace 名称不能包含
下划线，因此不能写成 `nanobot_test`。测试清单包含独立的 RBAC、local PV/PVC、Service 和
Deployment，可以与正式环境并存；测试 NodePort 为 `30081`：

```bash
kubectl label node <test-node-name> nanobot_node=test
kubectl apply -f k8s/base/container-up-test.yaml
kubectl rollout status deployment/container-up -n nanobot-test
```

kubectl delete ns nanobot && kubectl delete pv nanobot-data-pv-local nanobot-source-pv-local nanobot-common-pv-local
kubectl delete ns nanobot-test && kubectl delete pv nanobot-test-data-pv-local nanobot-test-source-pv-local nanobot-test-common-pv-local
kubectl delete ns nanobot && kubectl delete pv nanobot-data-pv nanobot-source-pv nanobot-common-pv
kubectl delete ns nanobot && kubectl delete pv minio-pv

测试环境的集群内地址为 `http://container-up.nanobot-test.svc.cluster.local:8080`。动态创建的
bucket runtime 会自动取得这个地址；web-server 等独立调用方则需要把自己的
`CONTAINER_UP_BASE_URL` 显式改为该地址。

查看日志：

```bash
kubectl logs -n nanobot deploy/container-up -f
kubectl logs -n nanobot deploy/nanobot-bucket-0 -f
kubectl logs -n nanobot deploy/engineering-scene-mcp -f

kubectl logs -n nanobot-test deploy/container-up -f
kubectl logs -n nanobot-test deploy/nanobot-test-bucket-0 -f
kubectl logs -n nanobot-test deploy/engineering-scene-mcp -f
```

web server测试：
说明：`attachments` 里如果已经是 `http/https` URL，直接按 URL 使用；不要下载到本地，也不要写入本地缓存。仅当附件是本地路径时，才走上传并替换为可访问 URL。

env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy curl -sS -X POST http://127.0.0.1:8090/inbound/web-wd -H 'Content-Type: application/json' -d '{"user_id":"web-demo-2","chat_id":"web-chat-2","content":"给我生成一个txt，里面写sucess，作为附件发给我","attachments":[],"metadata":{},"raw":{}}'

curl -sS -X POST 'http://192.168.199.166:30080/inbound/web-wd' \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "user_id": "web-demo-2",
  "chat_id": "web-chat-2",
  "content": "/new",
  "attachments": [],
  "metadata": {},
  "raw": {}
}
JSON

curl -sS -X POST 'http://192.168.102.189:30080/inbound/web-test' \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "user_id": "web-demo-2",
  "chat_id": "web-chat-2",
  "content": "/new",
  "attachments": [],
  "metadata": {},
  "raw": {}
}
JSON

curl -sS -X POST 'http://192.168.199.166:30080/inbound/web-wd' \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "user_id": "web-demo-2",
  "chat_id": "web-chat-2",
  "content": "/cancel",
  "attachments": [],
  "metadata": {},
  "raw": {}
}
JSON

curl -sS -X POST 'http://192.168.199.166:30080/inbound/web-main' \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "user_id": "web-demo-2",
  "chat_id": "web-chat-2",
  "content": "你是谁",
  "attachments": [],
  "metadata": {},
  "raw": {}
}
JSON

curl -sS -X POST 'http://192.168.199.166:30080/inbound/web-wd' \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "user_id": "web-demo-2",
  "chat_id": "web-chat-2",
  "content": "提交人孙宸，提交时间2026/05/20，分析图片中的安全风险",
  "attachments": [
    "http://192.168.199.166:9000/attachments/2026/05/19/3eeed76636d54c9c9e0252c6517e5cd0/tmp_c8c38602127542f09ea8336e65661a10.jpg","http://192.168.199.166:9000/attachments/2026/05/20/a835cde7fcf943ea9922023770ab0242/tmp_f79d82c30e97bfc19447817a3adc76b3.jpg"
  ],
  "metadata": {},
  "raw": {}
}
JSON


curl -sS -X POST 'http://127.0.0.1:8090/inbound/web-wd' \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "user_id": "web-demo-2",
  "chat_id": "web-chat-2",
  "content": "提交人孙宸，提交时间2026/05/20，分析视频中的安全风险",
  "attachments": [
    "http://192.168.199.166:9000/attachments/2026/05/28/4b134cb715764ba7b64f63063d67e7e1/1.mp4"
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
