# Nanobot 分桶架构 K8s 重构 SDD

> 说明：该 SDD 记录的是最初设计稿。当前分支的实际实现已经进一步调整：
> 1. `agent-gateway` 已并入统一的 `container_up` 网关。
> 2. frontend 公共资源改为 `frontends/frontends.json + common/<frontend>/config.json|skills|templates` 布局。
> 3. bucket runtime 相关本地 relay / 进程组逻辑已从 `nanobot/` 移到 `bucket_runtime/`。
> 4. bucket 部署模型已从“固定副本 StatefulSet + Headless Service”切换为“按需创建的 Deployment + Service”。
> 5. 调度依据已从“用户固定 bucket”切换为 `user_instances + buckets` 运行态表。
> 请以 [docs/PROJECT_ARCHITECTURE.md](docs/PROJECT_ARCHITECTURE.md) 和 [docs/K8S_BRANCH_LAYOUT.md](docs/K8S_BRANCH_LAYOUT.md) 为准。

版本：v1.0  
日期：2026-04-25  
文档类型：Software Design Document（SDD）  
适用对象：Nanobot 分桶运行、用户工作空间持久化、K8s 化部署改造

---

## 1. 背景与目标

### 1.1 当前架构背景

当前系统采用“网关 + 分桶容器 + 用户 Nanobot 实例”的运行模式。用户请求进入网关后，根据用户标识被路由到某个桶；每个桶内容纳多个用户 Nanobot 实例。实例处理完成后，不直接对外部渠道发送消息，而是调用网关提供的出站接口，由网关统一完成消息发送。

现有设计具备以下特点：

| 项目 | 当前约束 |
|---|---|
| 用户路由键 | `frontend_id + user_id` |
| 用户实例 | 一用户一 Nanobot 实例 |
| 分桶方式 | 一个桶内容纳多个用户实例 |
| Nanobot 启动方式 | 命令行启动 |
| 公共源码 | 公共路径挂载 |
| 公共 config | 公共路径挂载 |
| 公共 skill | 公共路径挂载 |
| 公共 template | 公共路径挂载 |
| 用户 workspace | 持久化保留 |
| workspace 初始化 | 首次创建时从公共 template 目录复制文件 |
| 实例释放 | 停止用户 Nanobot 进程，保留用户 workspace |
| 出站消息 | Nanobot 实例调用网关出站接口 |
| 出站鉴权 | 沿用现有实现，当前不加鉴权 |
| 存储方式 | 支持 NFS |

### 1.2 重构目标

本次重构目标是将现有分桶容器运行模式迁移到 K8s，使系统具备更好的部署标准化、跨节点扩展、健康检查、资源隔离和运维能力。

目标包括：

1. 保留当前 `frontend_id + user_id` 的用户路由方式。
2. 保留“一用户一 Nanobot 实例”的运行模型。
3. 保留“一个桶内多个用户实例”的资源复用模型。
4. 保留 Nanobot 实例调用网关出站接口的消息发送模式。
5. 使用 K8s StatefulSet 表达固定桶编号。
6. 使用 Headless Service 提供稳定桶地址。
7. 使用 NFS 挂载公共源码、config、skills、templates 和用户 workspaces。
8. 支持跨节点调度 bucket Pod。
9. 支持闲置实例释放，但不删除用户工作目录。
10. 为后续扩容、监控、日志、实例迁移和平台化管理预留接口。

---

## 2. 总体架构

### 2.1 架构总览

```text
外部渠道 / Web / 飞书 / HTTP
        ↓
agent-gateway
        ├── 接收入站请求
        ├── 使用 frontend_id + user_id 查询用户桶绑定
        ├── 新用户分配 bucket
        ├── 转发请求到固定 bucket Pod
        └── 提供出站接口
                ↓
nanobot-bucket StatefulSet
        ├── nanobot-bucket-0
        │     ├── user-a Nanobot 进程
        │     ├── user-b Nanobot 进程
        │     └── user-c Nanobot 进程
        ├── nanobot-bucket-1
        │     ├── user-d Nanobot 进程
        │     └── user-e Nanobot 进程
        └── nanobot-bucket-2
              └── user-f Nanobot 进程
                ↓
NFS 挂载
        ├── source      公共源码，只读
        ├── config      公共配置，只读
        ├── skills      公共 skill，只读
        ├── templates   公共模板，只读
        └── workspaces  用户工作空间，读写
```

### 2.2 运行逻辑

1. 外部请求进入 `agent-gateway`。
2. `agent-gateway` 从请求中提取 `frontend_id` 和 `user_id`。
3. 查询 `user_bucket_binding`。
4. 如果不存在绑定，则为该用户分配一个可用 bucket。
5. `agent-gateway` 拼接目标 bucket Pod 的稳定 DNS。
6. 请求转发到对应 bucket-runtime。
7. bucket-runtime 根据 `frontend_id + user_id` 查找用户 Nanobot 进程。
8. 如果进程不存在，则创建用户 workspace，复制模板，启动 Nanobot 命令行进程。
9. bucket-runtime 将请求转发给该用户 Nanobot 进程。
10. Nanobot 处理后调用 `agent-gateway` 出站接口。
11. `agent-gateway` 按现有逻辑发送出站消息。

---

## 3. K8s 资源设计

### 3.1 Namespace

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: nanobot
```

### 3.2 核心资源清单

| K8s 资源 | 名称 | 作用 |
|---|---|---|
| Deployment | `agent-gateway` | 入站路由与出站代理 |
| Service | `agent-gateway` | 暴露网关服务 |
| StatefulSet | `nanobot-bucket` | 分桶运行，每个副本一个 bucket |
| Headless Service | `nanobot-bucket` | 为每个 bucket Pod 提供稳定 DNS |
| PV/PVC | `nanobot-source` | 公共源码，只读挂载 |
| PV/PVC | `nanobot-config` | 公共配置，只读挂载 |
| PV/PVC | `nanobot-skills` | 公共 skills，只读挂载 |
| PV/PVC | `nanobot-templates` | 公共 templates，只读挂载 |
| PV/PVC | `nanobot-workspaces` | 用户 workspace，读写挂载 |
| PVC | `agent-gateway-data` | 网关本地数据，仅测试环境可用 |

---

## 4. NFS 存储设计

### 4.1 NFS 服务端目录规划

假设 NFS 服务端 IP：

```text
192.168.30.100
```

NFS 根目录：

```text
/data/nanobot-nfs
```

目录规划：

```text
/data/nanobot-nfs
  ├── source
  │   └── nanobot 源码
  ├── config
  │   └── config.json 等公共配置
  ├── skills
  │   └── 公共 skills
  ├── templates
  │   └── 公共模板文件
  └── workspaces
      └── 按 frontend_id/user_id 自动创建用户目录
```

### 4.2 NFS 服务端安装与配置

```bash
sudo apt update
sudo apt install -y nfs-kernel-server

sudo mkdir -p /data/nanobot-nfs/source
sudo mkdir -p /data/nanobot-nfs/config
sudo mkdir -p /data/nanobot-nfs/skills
sudo mkdir -p /data/nanobot-nfs/templates
sudo mkdir -p /data/nanobot-nfs/workspaces

sudo chown -R 1000:1000 /data/nanobot-nfs
sudo chmod -R 775 /data/nanobot-nfs
```

`/etc/exports` 配置：

```text
/data/nanobot-nfs/source     *(ro,sync,no_subtree_check,no_root_squash)
/data/nanobot-nfs/config     *(ro,sync,no_subtree_check,no_root_squash)
/data/nanobot-nfs/skills     *(ro,sync,no_subtree_check,no_root_squash)
/data/nanobot-nfs/templates  *(ro,sync,no_subtree_check,no_root_squash)
/data/nanobot-nfs/workspaces *(rw,sync,no_subtree_check,no_root_squash)
```

生效：

```bash
sudo exportfs -ra
sudo systemctl restart nfs-kernel-server
sudo exportfs -v
```

### 4.3 权限策略

| 目录 | 权限 | 说明 |
|---|---|---|
| source | ro | 运行时只读，避免实例修改源码 |
| config | ro | 公共配置只读 |
| skills | ro | skill 只读共享 |
| templates | ro | 模板只读，用户初始化时复制 |
| workspaces | rw | 用户实例需要读写 |

---

## 5. K8s PV/PVC 配置

### 5.1 source PV/PVC

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: nanobot-source-pv
spec:
  capacity:
    storage: 10Gi
  accessModes:
    - ReadOnlyMany
  nfs:
    server: 192.168.30.100
    path: /data/nanobot-nfs/source
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: nanobot-source-pvc
  namespace: nanobot
spec:
  accessModes:
    - ReadOnlyMany
  resources:
    requests:
      storage: 10Gi
  volumeName: nanobot-source-pv
```

### 5.2 config PV/PVC

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: nanobot-config-pv
spec:
  capacity:
    storage: 1Gi
  accessModes:
    - ReadOnlyMany
  nfs:
    server: 192.168.30.100
    path: /data/nanobot-nfs/config
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: nanobot-config-pvc
  namespace: nanobot
spec:
  accessModes:
    - ReadOnlyMany
  resources:
    requests:
      storage: 1Gi
  volumeName: nanobot-config-pv
```

### 5.3 skills PV/PVC

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: nanobot-skills-pv
spec:
  capacity:
    storage: 10Gi
  accessModes:
    - ReadOnlyMany
  nfs:
    server: 192.168.30.100
    path: /data/nanobot-nfs/skills
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: nanobot-skills-pvc
  namespace: nanobot
spec:
  accessModes:
    - ReadOnlyMany
  resources:
    requests:
      storage: 10Gi
  volumeName: nanobot-skills-pv
```

### 5.4 templates PV/PVC

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: nanobot-templates-pv
spec:
  capacity:
    storage: 5Gi
  accessModes:
    - ReadOnlyMany
  nfs:
    server: 192.168.30.100
    path: /data/nanobot-nfs/templates
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: nanobot-templates-pvc
  namespace: nanobot
spec:
  accessModes:
    - ReadOnlyMany
  resources:
    requests:
      storage: 5Gi
  volumeName: nanobot-templates-pv
```

### 5.5 workspaces PV/PVC

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: nanobot-workspaces-pv
spec:
  capacity:
    storage: 1Ti
  accessModes:
    - ReadWriteMany
  nfs:
    server: 192.168.30.100
    path: /data/nanobot-nfs/workspaces
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: nanobot-workspaces-pvc
  namespace: nanobot
spec:
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 1Ti
  volumeName: nanobot-workspaces-pv
```

---

## 6. agent-gateway 设计

### 6.1 组件职责

`agent-gateway` 负责系统入站和出站的统一管理，不直接运行 Nanobot 实例。

职责：

1. 接收外部入站请求。
2. 提取 `frontend_id` 和 `user_id`。
3. 查询用户 bucket 绑定。
4. 新用户分配 bucket。
5. 将请求转发到目标 bucket-runtime。
6. 提供出站接口，供用户 Nanobot 进程调用。
7. 按现有实现完成外部消息发送。
8. 记录用户绑定、入站、出站和路由日志。

### 6.2 代码结构建议

```text
agent_gateway/
  main.py
  api/
    inbound.py
    outbound.py
    health.py
  services/
    router_service.py
    bucket_allocator.py
    outbound_service.py
  repositories/
    binding_repository.py
    message_log_repository.py
  clients/
    bucket_client.py
  models/
    inbound.py
    outbound.py
    binding.py
  config.py
```

### 6.3 入站接口

```http
POST /inbound/{frontend_id}
```

请求体：

```json
{
  "user_id": "u001",
  "content": "用户消息内容",
  "raw": {}
}
```

标准化后：

```json
{
  "frontend_id": "feishu-main",
  "user_id": "u001",
  "content": "用户消息内容",
  "raw": {}
}
```

处理流程：

```text
1. 校验 frontend_id 和 user_id。
2. 查询 user_bucket_binding。
3. 如果不存在绑定，则调用 BucketAllocator 分配 bucket。
4. 保存绑定关系。
5. 拼接 bucket Pod DNS。
6. 转发请求到 bucket-runtime /inbound。
7. 返回 accepted。
```

返回：

```json
{
  "status": "accepted",
  "frontend_id": "feishu-main",
  "user_id": "u001",
  "bucket_id": 0
}
```

### 6.4 出站接口

沿用当前实现模式，不新增鉴权。

推荐接口：

```http
POST /outbound
```

请求体示例：

```json
{
  "frontend_id": "feishu-main",
  "user_id": "u001",
  "content": "处理结果",
  "raw": {}
}
```

处理流程：

```text
1. 接收 Nanobot 用户进程发来的出站请求。
2. 根据 frontend_id 找到对应外部渠道或前端。
3. 按现有实现发送消息。
4. 记录出站日志。
5. 返回发送结果。
```

### 6.5 用户绑定表

生产环境建议使用 PostgreSQL 或 MySQL。测试环境单副本网关可使用 SQLite。

```sql
CREATE TABLE user_bucket_binding (
    id BIGSERIAL PRIMARY KEY,
    frontend_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(256) NOT NULL,
    bucket_id INT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(frontend_id, user_id)
);
```

### 6.6 路由逻辑

```python
async def route_inbound(req):
    frontend_id = req.frontend_id
    user_id = req.user_id

    binding = await binding_repo.get(frontend_id, user_id)

    if binding is None:
        bucket_id = await bucket_allocator.allocate()
        binding = await binding_repo.create(frontend_id, user_id, bucket_id)

    target = build_bucket_url(binding.bucket_id)
    await bucket_client.forward(target, req)

    return {
        "status": "accepted",
        "frontend_id": frontend_id,
        "user_id": user_id,
        "bucket_id": binding.bucket_id,
    }
```

bucket 地址：

```python
def build_bucket_url(bucket_id: int) -> str:
    return (
        f"http://nanobot-bucket-{bucket_id}."
        f"nanobot-bucket.nanobot.svc.cluster.local:8080/inbound"
    )
```

### 6.7 新用户 bucket 分配策略

第一阶段可采用简单轮询或最小用户数分配。

推荐逻辑：

1. 读取当前 bucket 数量。
2. 查询每个 bucket 已绑定用户数量。
3. 选择用户数最少的 bucket。
4. 保存绑定。

```python
async def allocate() -> int:
    counts = await binding_repo.count_by_bucket()
    bucket_count = settings.bucket_count
    candidates = []

    for bucket_id in range(bucket_count):
        candidates.append((bucket_id, counts.get(bucket_id, 0)))

    candidates.sort(key=lambda x: x[1])
    return candidates[0][0]
```

---

## 7. bucket-runtime 设计

### 7.1 组件职责

`bucket-runtime` 是每个 bucket Pod 内的主进程，负责管理该桶内的多个用户 Nanobot 子进程。

职责：

1. 提供 `/inbound` 接口。
2. 根据 `frontend_id + user_id` 定位用户进程。
3. 不存在则启动用户 Nanobot 进程。
4. 初始化用户 workspace。
5. 从公共 templates 复制模板到用户 workspace。
6. 为每个用户进程分配本地端口。
7. 将请求转发给用户 Nanobot 进程。
8. 维护进程活跃时间。
9. 定时释放闲置进程。
10. 提供 bucket 状态接口。

### 7.2 代码结构建议

```text
bucket_runtime/
  main.py
  api/
    inbound.py
    health.py
    status.py
  core/
    process_manager.py
    command_builder.py
    port_allocator.py
    workspace_manager.py
    idle_reaper.py
  clients/
    nanobot_client.py
  models/
    inbound.py
    process.py
  config.py
```

### 7.3 bucket-runtime 接口

#### 健康检查

```http
GET /health/live
GET /health/ready
```

#### 入站接口

```http
POST /inbound
```

请求：

```json
{
  "frontend_id": "feishu-main",
  "user_id": "u001",
  "content": "用户消息",
  "raw": {}
}
```

处理流程：

```text
1. 生成 user_key = frontend_id + ':' + user_id。
2. 获取用户锁。
3. 检查用户 Nanobot 进程是否存在。
4. 不存在则初始化 workspace 并启动进程。
5. 将请求转发到用户进程本地端口。
6. 更新 last_active_at。
7. 返回 accepted。
```

#### 状态接口

```http
GET /bucket/status
```

返回：

```json
{
  "bucket_id": 0,
  "running_processes": 12,
  "max_processes": 30,
  "users": [
    {
      "frontend_id": "feishu-main",
      "user_id": "u001",
      "port": 20001,
      "last_active_at": 1710000000
    }
  ]
}
```

---

## 8. 用户 Nanobot 进程管理

### 8.1 进程模型

每个用户对应一个命令行启动的 Nanobot 进程。

```text
bucket-runtime
  ├── nanobot gateway --port 20001 --workspace /mnt/nanobot/workspaces/feishu-main/u001 --config /mnt/nanobot/config/config.json
  ├── nanobot gateway --port 20002 --workspace /mnt/nanobot/workspaces/feishu-main/u002 --config /mnt/nanobot/config/config.json
  └── nanobot gateway --port 20003 --workspace /mnt/nanobot/workspaces/web-main/u003 --config /mnt/nanobot/config/config.json
```

### 8.2 用户实例启动命令

根据仓库 CLI 实现，命令入口为：

```bash
nanobot gateway \
  --port ${USER_PORT} \
  --workspace /mnt/nanobot/workspaces/${FRONTEND_ID}/${USER_ID} \
  --config /mnt/nanobot/config/config.json
```

调试时可增加：

```bash
--verbose
```

完整示例：

```bash
nanobot gateway \
  --port 20001 \
  --workspace /mnt/nanobot/workspaces/feishu-main/user-001 \
  --config /mnt/nanobot/config/config.json
```

### 8.3 CommandBuilder

```python
class NanobotCommandBuilder:
    def __init__(
        self,
        config_path: str = "/mnt/nanobot/config/config.json",
        workspace_root: str = "/mnt/nanobot/workspaces",
    ):
        self.config_path = config_path
        self.workspace_root = workspace_root

    def build(
        self,
        frontend_id: str,
        user_id: str,
        port: int,
        verbose: bool = False,
    ) -> list[str]:
        workspace = f"{self.workspace_root}/{frontend_id}/{user_id}"

        cmd = [
            "nanobot",
            "gateway",
            "--port",
            str(port),
            "--workspace",
            workspace,
            "--config",
            self.config_path,
        ]

        if verbose:
            cmd.append("--verbose")

        return cmd
```

### 8.4 进程数据结构

```python
@dataclass
class UserProcess:
    frontend_id: str
    user_id: str
    user_key: str
    workspace_dir: str
    port: int
    process: subprocess.Popen
    started_at: float
    last_active_at: float
```

### 8.5 ProcessManager

```python
class ProcessManager:
    def __init__(self):
        self.processes: dict[str, UserProcess] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    async def get_or_start(self, frontend_id: str, user_id: str) -> UserProcess:
        user_key = f"{frontend_id}:{user_id}"
        lock = self.locks.setdefault(user_key, asyncio.Lock())

        async with lock:
            if user_key in self.processes:
                p = self.processes[user_key]
                p.last_active_at = time.time()
                return p

            workspace = workspace_manager.ensure_workspace(frontend_id, user_id)
            port = port_allocator.allocate(user_key)
            cmd = command_builder.build(frontend_id, user_id, port)

            proc = subprocess.Popen(
                cmd,
                cwd="/mnt/nanobot/source",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            user_process = UserProcess(
                frontend_id=frontend_id,
                user_id=user_id,
                user_key=user_key,
                workspace_dir=str(workspace),
                port=port,
                process=proc,
                started_at=time.time(),
                last_active_at=time.time(),
            )

            self.processes[user_key] = user_process
            return user_process
```

---

## 9. 端口分配设计

### 9.1 端口范围

```text
NANOBOT_PORT_START=20000
NANOBOT_PORT_END=29999
```

### 9.2 PortAllocator

```python
class PortAllocator:
    def __init__(self, start: int = 20000, end: int = 29999):
        self.free_ports = set(range(start, end + 1))
        self.used_ports: dict[str, int] = {}

    def allocate(self, user_key: str) -> int:
        if user_key in self.used_ports:
            return self.used_ports[user_key]
        if not self.free_ports:
            raise RuntimeError("no free port available")
        port = self.free_ports.pop()
        self.used_ports[user_key] = port
        return port

    def release(self, user_key: str):
        port = self.used_ports.pop(user_key, None)
        if port is not None:
            self.free_ports.add(port)
```

### 9.3 端口暴露策略

用户 Nanobot 进程只监听 bucket Pod 内部本地地址：

```text
127.0.0.1:${USER_PORT}
```

不暴露到 Pod 外部。外部只能访问 bucket-runtime 的 `8080` 端口。

---

## 10. 用户 workspace 初始化设计

### 10.1 目录结构

用户 workspace 根目录：

```text
/mnt/nanobot/workspaces/{frontend_id}/{user_id}
```

示例：

```text
/mnt/nanobot/workspaces/feishu-main/user-001
```

### 10.2 初始化规则

首次创建用户 workspace 时：

1. 创建用户目录。
2. 将 `/mnt/nanobot/templates` 下文件复制到用户 workspace。
3. 写入 `.workspace_initialized` 标记文件。
4. 后续启动不重复覆盖。

### 10.3 WorkspaceManager

```python
class WorkspaceManager:
    def __init__(
        self,
        workspace_root: str = "/mnt/nanobot/workspaces",
        templates_root: str = "/mnt/nanobot/templates",
    ):
        self.workspace_root = Path(workspace_root)
        self.templates_root = Path(templates_root)

    def ensure_workspace(self, frontend_id: str, user_id: str) -> Path:
        workspace = self.workspace_root / self.safe(frontend_id) / self.safe(user_id)
        flag = workspace / ".workspace_initialized"

        workspace.mkdir(parents=True, exist_ok=True)

        if not flag.exists():
            self.copy_templates(self.templates_root, workspace)
            flag.write_text("true", encoding="utf-8")

        return workspace

    def copy_templates(self, src: Path, dst: Path):
        for item in src.iterdir():
            target = dst / item.name
            if target.exists():
                continue
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)

    def safe(self, value: str) -> str:
        return value.replace("/", "_").replace("\\", "_").replace("..", "_")
```

---

## 11. 闲置实例释放设计

### 11.1 释放策略

用户进程闲置超过 TTL 后释放。

配置：

```text
INSTANCE_IDLE_TTL_SECONDS=1800
INSTANCE_STOP_GRACE_SECONDS=10
INSTANCE_EVICT_INTERVAL_SECONDS=60
MAX_PROCESSES_PER_BUCKET=30
```

释放动作：

1. 检查用户进程最后活跃时间。
2. 超过 TTL 则发送 `SIGTERM`。
3. 等待 `INSTANCE_STOP_GRACE_SECONDS`。
4. 未退出则发送 `SIGKILL`。
5. 释放端口。
6. 删除内存中的进程记录。
7. 保留用户 workspace。

### 11.2 IdleReaper

```python
async def reap_idle_processes(self):
    now = time.time()
    for user_key, p in list(self.processes.items()):
        idle = now - p.last_active_at
        if idle < self.idle_ttl:
            continue

        await self.stop_process(user_key)

async def stop_process(self, user_key: str):
    p = self.processes.get(user_key)
    if not p:
        return

    p.process.terminate()
    try:
        await asyncio.wait_for(wait_process_exit(p.process), timeout=self.stop_grace)
    except asyncio.TimeoutError:
        p.process.kill()

    self.port_allocator.release(user_key)
    self.processes.pop(user_key, None)
```

---

## 12. bucket-runtime 转发设计

### 12.1 请求链路

```text
agent-gateway
  ↓
POST http://nanobot-bucket-0.nanobot-bucket.nanobot.svc.cluster.local:8080/inbound
  ↓
bucket-runtime
  ↓
POST http://127.0.0.1:20001/...
  ↓
用户 Nanobot 进程
```

### 12.2 转发实现

实际转发路径需参考现有 Nanobot 用户实例的 HTTP 接口。如果用户进程暴露的接口与 gateway 相同，可直接转发原始请求。

示例：

```python
async def forward_to_user_process(user_process: UserProcess, payload: dict):
    url = f"http://127.0.0.1:{user_process.port}/inbound"
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()
```

如果现有 Nanobot gateway 没有 `/inbound` 形式接口，需要在 bucket-runtime 中适配到现有 org server 接口。

---

## 13. K8s 部署 YAML

### 13.1 Headless Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: nanobot-bucket
  namespace: nanobot
spec:
  clusterIP: None
  selector:
    app: nanobot-bucket
  ports:
    - name: http
      port: 8080
      targetPort: 8080
```

### 13.2 nanobot-bucket StatefulSet

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: nanobot-bucket
  namespace: nanobot
spec:
  serviceName: nanobot-bucket
  replicas: 4
  selector:
    matchLabels:
      app: nanobot-bucket
  template:
    metadata:
      labels:
        app: nanobot-bucket
    spec:
      containers:
        - name: bucket-runtime
          image: nanobot-bucket-runtime:v1.0.0
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 8080
          env:
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: WORKSPACE_ROOT
              value: "/mnt/nanobot/workspaces"
            - name: SOURCE_ROOT
              value: "/mnt/nanobot/source"
            - name: CONFIG_ROOT
              value: "/mnt/nanobot/config"
            - name: SKILLS_ROOT
              value: "/mnt/nanobot/skills"
            - name: TEMPLATES_ROOT
              value: "/mnt/nanobot/templates"
            - name: CONFIG_PATH
              value: "/mnt/nanobot/config/config.json"
            - name: OUTBOUND_GATEWAY_URL
              value: "http://agent-gateway.nanobot.svc.cluster.local:8080/outbound"
            - name: INSTANCE_IDLE_TTL_SECONDS
              value: "1800"
            - name: INSTANCE_STOP_GRACE_SECONDS
              value: "10"
            - name: INSTANCE_EVICT_INTERVAL_SECONDS
              value: "60"
            - name: MAX_PROCESSES_PER_BUCKET
              value: "30"
            - name: NANOBOT_PORT_START
              value: "20000"
            - name: NANOBOT_PORT_END
              value: "29999"
          volumeMounts:
            - name: source
              mountPath: /mnt/nanobot/source
              readOnly: true
            - name: config
              mountPath: /mnt/nanobot/config
              readOnly: true
            - name: skills
              mountPath: /mnt/nanobot/skills
              readOnly: true
            - name: templates
              mountPath: /mnt/nanobot/templates
              readOnly: true
            - name: workspaces
              mountPath: /mnt/nanobot/workspaces
          readinessProbe:
            httpGet:
              path: /health/ready
              port: 8080
            initialDelaySeconds: 5
            periodSeconds: 5
          livenessProbe:
            httpGet:
              path: /health/live
              port: 8080
            initialDelaySeconds: 20
            periodSeconds: 10
          resources:
            requests:
              cpu: "2"
              memory: "4Gi"
            limits:
              cpu: "8"
              memory: "16Gi"
      volumes:
        - name: source
          persistentVolumeClaim:
            claimName: nanobot-source-pvc
        - name: config
          persistentVolumeClaim:
            claimName: nanobot-config-pvc
        - name: skills
          persistentVolumeClaim:
            claimName: nanobot-skills-pvc
        - name: templates
          persistentVolumeClaim:
            claimName: nanobot-templates-pvc
        - name: workspaces
          persistentVolumeClaim:
            claimName: nanobot-workspaces-pvc
```

### 13.3 agent-gateway Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: agent-gateway
  namespace: nanobot
spec:
  selector:
    app: agent-gateway
  ports:
    - name: http
      port: 8080
      targetPort: 8080
```

### 13.4 agent-gateway Deployment

生产建议使用 PostgreSQL/MySQL。以下示例以环境变量 `DB_DSN` 表示数据库连接。

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent-gateway
  namespace: nanobot
spec:
  replicas: 2
  selector:
    matchLabels:
      app: agent-gateway
  template:
    metadata:
      labels:
        app: agent-gateway
    spec:
      containers:
        - name: agent-gateway
          image: agent-gateway:v1.0.0
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 8080
          env:
            - name: DB_DSN
              value: "postgresql://user:password@postgres.nanobot.svc.cluster.local:5432/nanobot"
            - name: BUCKET_SERVICE_NAME
              value: "nanobot-bucket"
            - name: BUCKET_STATEFULSET_NAME
              value: "nanobot-bucket"
            - name: BUCKET_NAMESPACE
              value: "nanobot"
            - name: BUCKET_COUNT
              value: "4"
          readinessProbe:
            httpGet:
              path: /health/ready
              port: 8080
            initialDelaySeconds: 5
            periodSeconds: 5
          livenessProbe:
            httpGet:
              path: /health/live
              port: 8080
            initialDelaySeconds: 20
            periodSeconds: 10
          resources:
            requests:
              cpu: "500m"
              memory: "512Mi"
            limits:
              cpu: "2"
              memory: "2Gi"
```

---

## 14. 镜像设计

### 14.1 bucket-runtime 镜像

由于源码通过 NFS 公共挂载，bucket-runtime 镜像只需要包含：

1. bucket-runtime 代码。
2. Python 运行环境。
3. Nanobot 运行依赖。
4. `nanobot` 命令可用的 Python 环境。

Dockerfile 示例：

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bucket_runtime /app/bucket_runtime

ENV PYTHONPATH=/app:/mnt/nanobot/source

CMD ["python", "-m", "bucket_runtime.main"]
```

如果公共源码目录中是可编辑安装形式，也可以在容器启动时执行：

```bash
pip install -e /mnt/nanobot/source
```

但生产不建议每次 Pod 启动都安装依赖，建议依赖提前打进镜像。

### 14.2 agent-gateway 镜像

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent_gateway /app/agent_gateway

ENV PYTHONPATH=/app

CMD ["python", "-m", "agent_gateway.main"]
```

---

## 15. 部署步骤

### 15.1 准备 NFS

```bash
sudo mkdir -p /data/nanobot-nfs/source
sudo mkdir -p /data/nanobot-nfs/config
sudo mkdir -p /data/nanobot-nfs/skills
sudo mkdir -p /data/nanobot-nfs/templates
sudo mkdir -p /data/nanobot-nfs/workspaces
```

将源码、配置、skills、templates 放到对应目录：

```bash
cp -r nanobot/* /data/nanobot-nfs/source/
cp config.json /data/nanobot-nfs/config/config.json
cp -r skills/* /data/nanobot-nfs/skills/
cp -r templates/* /data/nanobot-nfs/templates/
```

### 15.2 应用 K8s 基础资源

```bash
kubectl apply -f namespace.yaml
kubectl apply -f pv-pvc.yaml
```

### 15.3 初始化数据库

```bash
psql -f schema.sql
```

### 15.4 部署 agent-gateway

```bash
kubectl apply -f agent-gateway.yaml
kubectl get pods -n nanobot -l app=agent-gateway
```

### 15.5 部署 bucket StatefulSet

```bash
kubectl apply -f nanobot-bucket-service.yaml
kubectl apply -f nanobot-bucket-statefulset.yaml
kubectl get pods -n nanobot -l app=nanobot-bucket
```

### 15.6 验证 bucket DNS

```bash
kubectl exec -it -n nanobot deploy/agent-gateway -- sh
curl http://nanobot-bucket-0.nanobot-bucket.nanobot.svc.cluster.local:8080/health/ready
```

### 15.7 发送测试请求

```bash
curl -X POST \
  http://agent-gateway.nanobot.svc.cluster.local:8080/inbound/feishu-main \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user-001",
    "content": "测试消息",
    "raw": {}
  }'
```

预期返回：

```json
{
  "status": "accepted",
  "frontend_id": "feishu-main",
  "user_id": "user-001",
  "bucket_id": 0
}
```

---

## 16. 验收标准

### 16.1 基础部署验收

| 验收项 | 标准 |
|---|---|
| namespace | `nanobot` 创建成功 |
| NFS PV/PVC | 所有 PVC 为 Bound |
| gateway | Pod Ready |
| bucket | StatefulSet 所有副本 Ready |
| DNS | 可访问 `nanobot-bucket-0.nanobot-bucket.nanobot.svc.cluster.local` |
| 挂载 | bucket Pod 内存在 `/mnt/nanobot/source/config/skills/templates/workspaces` |

### 16.2 用户路由验收

| 场景 | 标准 |
|---|---|
| 新用户首次请求 | 自动分配 bucket |
| 老用户再次请求 | 路由到同一 bucket |
| 不同用户请求 | 可分配到不同 bucket |
| `frontend_id + user_id` 相同 | 始终命中同一绑定 |

### 16.3 用户进程验收

| 场景 | 标准 |
|---|---|
| 首次请求 | 创建 workspace，复制模板，启动 Nanobot 进程 |
| 二次请求 | 复用已有 Nanobot 进程 |
| 闲置超时 | 进程被释放 |
| 闲置释放后再次请求 | 重新启动进程，workspace 保留 |
| Pod 重启 | workspace 保留，用户请求后重新启动进程 |

### 16.4 公共挂载验收

| 目录 | 标准 |
|---|---|
| source | Pod 内只读可见 |
| config | Pod 内只读可见 |
| skills | Pod 内只读可见 |
| templates | Pod 内只读可见，可复制到用户 workspace |
| workspaces | Pod 内可读写 |

### 16.5 出站验收

| 场景 | 标准 |
|---|---|
| 用户进程调用 `/outbound` | gateway 能收到请求 |
| gateway 出站 | 按现有实现发送消息 |
| 出站失败 | 有错误日志 |

---

## 17. 风险与注意事项

### 17.1 公共源码 NFS 挂载风险

公共源码通过 NFS 挂载虽然便于更新，但存在运行时一致性风险。

建议按版本目录组织：

```text
/data/nanobot-nfs/source/v1.0.0
/data/nanobot-nfs/source/v1.0.1
/data/nanobot-nfs/skills/v1.0.0
/data/nanobot-nfs/templates/v1.0.0
```

不要直接在运行中修改当前版本目录。

### 17.2 多副本网关数据库风险

如果 `agent-gateway replicas > 1`，不建议多个 Pod 共享 NFS 上的 SQLite。

生产建议使用：

```text
PostgreSQL / MySQL
```

### 17.3 子进程日志阻塞风险

`subprocess.PIPE` 必须有后台任务持续消费，否则子进程输出过多可能阻塞。

建议：

```python
async def pipe_process_logs(proc, user_key):
    for line in proc.stdout:
        logger.info("[%s] %s", user_key, line.rstrip())
```

### 17.4 同用户并发风险

同一用户请求建议串行处理。不同用户可以并行。

```text
不同用户：并行
同一用户：串行
```

### 17.5 workspace 不得误删

实例释放只停止进程，不删除 workspace。

禁止在 idle reaper 中执行：

```bash
rm -rf /mnt/nanobot/workspaces/{frontend_id}/{user_id}
```

---

## 18. 后续演进

第一阶段完成 K8s 化与分桶运行后，可继续演进：

1. bucket 运行状态心跳上报。
2. gateway 根据 bucket 实时负载分配新用户。
3. bucket 自动扩容。
4. 用户从高负载 bucket 迁移到低负载 bucket。
5. 公共 source/config/skills/templates 版本化管理。
6. 出站接口增加内部鉴权。
7. Prometheus 指标接入。
8. 日志接入 Loki/ELK。
9. 附件和大文件迁移到 MinIO。
10. 从“用户固定 bucket”演进到“请求级实例池”。

---

## 19. 最终结论

本方案采用：

```text
agent-gateway + nanobot-bucket StatefulSet + NFS 公共挂载 + 用户 workspace 持久化
```

其中：

1. `agent-gateway` 保留现有入站和出站模式。
2. 用户路由键简化为 `frontend_id + user_id`。
3. `nanobot-bucket` 每个 Pod 是一个固定 bucket。
4. bucket 内通过命令行启动多个用户 Nanobot 进程。
5. 每个用户进程使用独立 workspace。
6. 公共源码、config、skills、templates 均通过 NFS 只读挂载到每个 bucket。
7. 用户 workspace 通过 NFS 读写挂载并持久化。
8. 闲置实例释放只停止进程，不删除 workspace。
9. K8s 负责 bucket Pod 生命周期、健康检查、跨节点调度和资源隔离。
