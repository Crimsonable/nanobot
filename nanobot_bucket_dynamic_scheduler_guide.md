# Nanobot K8s 分支 Bucket 动态调度改造指导书

## 1. 改造目标

当前 k8s 分支中，`bucket` 不应与用户永久绑定，而应作为承载用户实例的运行资源池。用户只稳定绑定自己的工作空间，在线期间临时绑定某个 bucket。

目标逻辑：

1. 根据 `user_id` 查询数据库表，表中记录用户实例状态（`online` / `destroyed` / `creating` / `error`）以及用户工作路径。
2. 如果用户实例在线，且记录中存在当前实例所在的 `bucket_id`，则直接根据 `bucket_id` 调用对应 bucket 内的用户实例。
3. 如果用户实例销毁或未创建，则获取或创建用户工作路径，再查询当前已有 bucket 的实例数量是否超过设定阈值。
4. 如果存在未超过阈值的 bucket，则在该空闲 bucket 中创建用户实例，并更新用户实例状态与 bucket 信息。
5. 如果所有 bucket 都超过或达到阈值，则创建新 bucket，再创建用户实例，并更新用户实例状态与 bucket 信息。
6. 用户实例空闲销毁时，更新实例状态为 `destroyed`，清除 bucket 信息，但保留用户工作路径。

---

## 2. 核心设计原则

### 2.1 用户只绑定工作空间

用户和工作空间的关系是长期关系：

```text
user_id -> workspace_path
```

用户和 bucket 的关系是运行期关系：

```text
user_id -> bucket_id
```

用户实例销毁后：

```text
workspace_path 保留
bucket_id 清空
status 更新为 destroyed
```

### 2.2 bucket 是运行资源池

bucket 不再表示某个用户的专属容器，而是一个可承载多个用户实例的运行单元。

```text
bucket-0
  ├── user-a instance
  ├── user-b instance
  └── user-c instance

bucket-1
  ├── user-d instance
  └── user-e instance
```

### 2.3 数据库是调度依据

不要只通过 Kubernetes Pod 数量判断 bucket 是否可用。Kubernetes 只能判断 bucket 容器是否存在和是否 Ready，不能准确表示 bucket 内有多少用户实例。

容量调度应以数据库中的 `current_instances` 和 `max_instances` 为准。

---

## 3. 推荐改造范围

建议按职责拆分以下模块，实际路径可根据当前仓库结构调整：

```text
container_up/
  ├── main.py / server.py / app.py
  ├── config.py
  ├── db.py
  ├── container_manager.py
  ├── bucket_manager.py        # 建议新增：负责 K8s bucket 创建、查询、删除
  ├── bucket_scheduler.py      # 建议新增：负责 bucket 分配与用户实例调度
  ├── workspace_manager.py     # 建议新增或扩展：负责用户工作空间
  └── models.py / schema.py    # 建议新增或扩展：定义数据结构

k8s/
  ├── deployment.yaml
  ├── service.yaml
  ├── rbac.yaml                # 建议新增或扩展
  └── bucket-template.yaml     # 可选：作为动态创建 bucket 的模板参考
```

关键要求：

```text
所有用户实例创建入口，都应统一调用 BucketScheduler。
不要在接口函数里直接创建 bucket 或直接创建用户实例。
```

---

## 4. 数据库表设计

### 4.1 用户实例表

建议新增或改造 `user_instances` 表。

```sql
CREATE TABLE IF NOT EXISTS user_instances (
    user_id TEXT PRIMARY KEY,
    workspace_path TEXT NOT NULL,

    status TEXT NOT NULL,
    bucket_id TEXT,
    instance_id TEXT,

    frontend_id TEXT,
    app_id TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_active_at TEXT
);
```

字段说明：

| 字段 | 含义 |
|---|---|
| `user_id` | 用户唯一标识 |
| `workspace_path` | 用户工作空间路径，稳定保留 |
| `status` | 用户实例状态，建议使用 `creating`、`online`、`destroyed`、`error` |
| `bucket_id` | 当前在线实例所在 bucket，销毁后清空 |
| `instance_id` | bucket 内部创建的用户实例 ID，可与 `user_id` 一致 |
| `frontend_id` | 前端应用 ID |
| `app_id` | 应用 ID，如当前系统有应用维度则保留 |
| `created_at` | 首次创建时间 |
| `updated_at` | 最近更新时间 |
| `last_active_at` | 最近活跃时间，用于空闲回收 |

销毁后的记录建议保留为：

```text
user_id = 原用户 ID
workspace_path = 原工作空间路径
status = destroyed
bucket_id = NULL
instance_id = NULL
```

### 4.2 bucket 表

建议新增 `buckets` 表。

```sql
CREATE TABLE IF NOT EXISTS buckets (
    bucket_id TEXT PRIMARY KEY,
    bucket_name TEXT NOT NULL,
    namespace TEXT NOT NULL,

    status TEXT NOT NULL,
    current_instances INTEGER NOT NULL DEFAULT 0,
    max_instances INTEGER NOT NULL,

    service_host TEXT,
    service_port INTEGER,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

字段说明：

| 字段 | 含义 |
|---|---|
| `bucket_id` | bucket 逻辑 ID，例如 `bucket-0` |
| `bucket_name` | Kubernetes 中的 Deployment/Service 名称，例如 `nanobot-bucket-0` |
| `namespace` | Kubernetes 命名空间 |
| `status` | bucket 状态，建议 `creating`、`running`、`full`、`idle`、`terminating`、`error` |
| `current_instances` | 当前承载的在线用户实例数量 |
| `max_instances` | 单个 bucket 最大承载实例数 |
| `service_host` | bucket 服务地址，例如 `http://nanobot-bucket-0:8080` |
| `service_port` | bucket 服务端口 |
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |

### 4.3 状态枚举

用户实例状态：

```text
creating
online
destroyed
error
```

bucket 状态：

```text
creating
running
full
idle
terminating
error
```

---

## 5. 配置项改造

建议在 `container_up.json` 或环境变量中增加 bucket 调度配置。

### 5.1 JSON 配置示例

```json
{
  "bucket": {
    "enabled": true,
    "namespace": "nanobot",
    "name_prefix": "nanobot-bucket",
    "image": "nanobot-bucket:latest",
    "container_port": 8080,
    "service_port": 8080,
    "max_instances_per_bucket": 20,
    "idle_ttl_seconds": 600,
    "workspace_root": "/app/nanobot_workspaces"
  }
}
```

### 5.2 环境变量示例

```bash
BUCKET_ENABLED=true
BUCKET_NAMESPACE=nanobot
BUCKET_NAME_PREFIX=nanobot-bucket
BUCKET_IMAGE=nanobot-bucket:latest
BUCKET_CONTAINER_PORT=8080
BUCKET_SERVICE_PORT=8080
BUCKET_MAX_INSTANCES_PER_BUCKET=20
BUCKET_IDLE_TTL_SECONDS=600
BUCKET_WORKSPACE_ROOT=/app/nanobot_workspaces
```

---

## 6. 核心模块职责

### 6.1 WorkspaceManager

负责用户工作空间路径管理。

职责：

```text
1. 根据 user_id 生成稳定工作路径
2. 查询数据库中是否已有 workspace_path
3. 如果没有，则创建目录并写入数据库
4. 用户实例销毁时不删除 workspace_path
```

示例接口：

```python
class WorkspaceManager:
    def get_or_create_workspace(self, user_id: str) -> str:
        ...
```

路径建议：

```text
/app/nanobot_workspaces/{safe_user_id}
```

注意：`user_id` 中可能包含 `/`、`:`、空格等特殊字符，需要做安全转换。

### 6.2 BucketManager

负责和 Kubernetes 交互。

职责：

```text
1. 创建 bucket Deployment 或 Pod
2. 创建 bucket Service
3. 查询 bucket 是否 Ready
4. 删除或缩容 bucket
```

示例接口：

```python
class BucketManager:
    def ensure_bucket_exists(self, bucket_id: str) -> None:
        ...

    def wait_bucket_ready(self, bucket_id: str, timeout: int = 60) -> None:
        ...

    def get_bucket_url(self, bucket_id: str) -> str:
        ...

    def delete_bucket(self, bucket_id: str) -> None:
        ...
```

建议每个 bucket 创建一组 Kubernetes 资源：

```text
Deployment: nanobot-bucket-0
Service:    nanobot-bucket-0
```

同 namespace 内访问地址：

```text
http://nanobot-bucket-0:8080
```

### 6.3 BucketScheduler

负责 bucket 分配逻辑，是本次改造的核心。

职责：

```text
1. 查询用户实例状态
2. 判断是否复用已有在线实例
3. 选择未满 bucket
4. 必要时创建新 bucket
5. 更新用户实例状态
6. 更新 bucket 实例数量
7. 处理并发锁和事务
```

示例接口：

```python
class BucketScheduler:
    def get_or_create_user_instance(
        self,
        user_id: str,
        frontend_id: str | None = None,
        app_id: str | None = None,
    ) -> UserInstanceRuntime:
        ...

    def release_user_instance(self, user_id: str) -> None:
        ...
```

返回结构建议：

```python
from dataclasses import dataclass

@dataclass
class UserInstanceRuntime:
    user_id: str
    workspace_path: str
    bucket_id: str
    bucket_url: str
    instance_id: str
```

---

## 7. 用户实例创建流程

### 7.1 目标流程

```text
收到用户请求
  ↓
根据 user_id 查询 user_instances 表
  ↓
如果 status = online 且 bucket_id 不为空
  ↓
    直接返回 bucket_url + instance_id
  ↓
否则
  ↓
    获取或创建 workspace_path
  ↓
    查询未满 bucket
  ↓
    如果有未满 bucket
        使用该 bucket
    否则
        创建新 bucket
  ↓
    在 bucket 中创建用户实例
  ↓
    更新 user_instances 表
  ↓
    更新 buckets.current_instances
  ↓
返回 bucket_url + instance_id
```

### 7.2 伪代码

```python
def get_or_create_user_instance(user_id: str, frontend_id: str | None, app_id: str | None):
    existing = db.get_user_instance(user_id)

    if existing and existing.status == "online" and existing.bucket_id:
        bucket = db.get_bucket(existing.bucket_id)
        return UserInstanceRuntime(
            user_id=user_id,
            workspace_path=existing.workspace_path,
            bucket_id=existing.bucket_id,
            bucket_url=bucket.service_host,
            instance_id=existing.instance_id,
        )

    workspace_path = workspace_manager.get_or_create_workspace(user_id)

    with db.transaction_immediate():
        bucket = db.find_available_bucket_for_update()

        if bucket is None:
            bucket = db.create_next_bucket_record(
                name_prefix=config.bucket.name_prefix,
                namespace=config.bucket.namespace,
                max_instances=config.bucket.max_instances_per_bucket,
                service_port=config.bucket.service_port,
            )

        db.increment_bucket_instances(bucket.bucket_id)

        db.upsert_user_instance(
            user_id=user_id,
            workspace_path=workspace_path,
            status="creating",
            bucket_id=bucket.bucket_id,
            instance_id=user_id,
            frontend_id=frontend_id,
            app_id=app_id,
        )

    try:
        bucket_manager.ensure_bucket_exists(bucket.bucket_id)
        bucket_manager.wait_bucket_ready(bucket.bucket_id)

        bucket_client.create_user_instance(
            bucket_url=bucket.service_host,
            user_id=user_id,
            instance_id=user_id,
            workspace_path=workspace_path,
            frontend_id=frontend_id,
            app_id=app_id,
        )

        db.update_user_instance_status(user_id, "online")
        db.update_bucket_status_by_count(bucket.bucket_id)

        return UserInstanceRuntime(
            user_id=user_id,
            workspace_path=workspace_path,
            bucket_id=bucket.bucket_id,
            bucket_url=bucket.service_host,
            instance_id=user_id,
        )

    except Exception:
        db.update_user_instance_status(user_id, "error")
        db.clear_user_instance_bucket(user_id)
        db.decrement_bucket_instances(bucket.bucket_id)
        db.update_bucket_status_by_count(bucket.bucket_id)
        raise
```

---

## 8. bucket 选择逻辑

### 8.1 查询未满 bucket

SQL 示例：

```sql
SELECT *
FROM buckets
WHERE status IN ('running', 'idle')
  AND current_instances < max_instances
ORDER BY current_instances ASC, created_at ASC
LIMIT 1;
```

推荐优先选择：

```text
1. current_instances 最少的 bucket
2. 如果数量相同，选择创建时间更早的 bucket
```

这样可以避免某一个 bucket 被持续压满。

### 8.2 创建新 bucket 记录

```python
def create_next_bucket_record():
    next_index = db.get_next_bucket_index()
    bucket_id = f"bucket-{next_index}"
    bucket_name = f"{name_prefix}-{next_index}"

    db.insert_bucket(
        bucket_id=bucket_id,
        bucket_name=bucket_name,
        namespace=namespace,
        status="creating",
        current_instances=0,
        max_instances=max_instances_per_bucket,
        service_host=f"http://{bucket_name}:{service_port}",
        service_port=service_port,
    )

    return db.get_bucket(bucket_id)
```

---

## 9. 并发控制要求

必须处理并发，否则会出现 bucket 超分配。

错误场景：

```text
bucket-0 current_instances = 19
max_instances = 20

请求 A 查询到 bucket-0 可用
请求 B 也查询到 bucket-0 可用

A +1
B +1

最终 current_instances = 21
```

### 9.1 SQLite 场景

如果当前项目使用 SQLite，建议在分配 bucket 时使用 `BEGIN IMMEDIATE`。

```python
from contextlib import contextmanager

@contextmanager
def transaction_immediate(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise
```

分配 bucket、增加计数、更新用户实例状态必须在同一个事务内完成。

### 9.2 Postgres 场景

如果后续迁移到 Postgres，可使用：

```sql
SELECT *
FROM buckets
WHERE status IN ('running', 'idle')
  AND current_instances < max_instances
ORDER BY current_instances ASC, created_at ASC
LIMIT 1
FOR UPDATE;
```

---

## 10. 用户实例销毁流程

### 10.1 目标流程

```text
用户实例空闲超时
  ↓
调用 bucket 删除用户实例接口
  ↓
user_instances.status 更新为 destroyed
  ↓
清除 user_instances.bucket_id
  ↓
清除 user_instances.instance_id
  ↓
buckets.current_instances - 1
  ↓
如果 bucket current_instances = 0
      bucket 状态更新为 idle
  ↓
如果 bucket idle 超过 TTL
      删除或缩容 bucket
```

### 10.2 伪代码

```python
def release_user_instance(user_id: str):
    instance = db.get_user_instance(user_id)

    if not instance:
        return

    if instance.status != "online":
        return

    bucket = db.get_bucket(instance.bucket_id)

    try:
        bucket_client.destroy_user_instance(
            bucket_url=bucket.service_host,
            instance_id=instance.instance_id,
        )
    finally:
        with db.transaction_immediate():
            db.update_user_instance_destroyed(
                user_id=user_id,
                clear_bucket=True,
            )

            db.decrement_bucket_instances(bucket.bucket_id)
            db.update_bucket_status_by_count(bucket.bucket_id)
```

---

## 11. bucket 空闲回收机制

建议不要在 bucket 为空时立即删除，而是增加一个定时任务或后台协程，定期扫描空闲 bucket。

### 11.1 空闲判断

```sql
SELECT *
FROM buckets
WHERE status = 'idle'
  AND current_instances = 0
  AND updated_at < datetime('now', '-600 seconds');
```

### 11.2 回收策略

策略一：删除 bucket。

```text
删除 Deployment
删除 Service
删除 buckets 记录或标记 destroyed
```

策略二：保留 bucket，但 scale 到 0。

```text
Deployment replicas = 0
Service 保留
bucket 状态 = idle
```

当前阶段建议：

```text
空闲超过 idle_ttl_seconds 后 scale 到 0
```

这样比直接删除更稳，调试也更方便。

---

## 12. Kubernetes 资源创建方式

### 12.1 推荐使用 Deployment + Service

不建议每个 bucket 使用 StatefulSet，除非 bucket 自身需要独立持久卷。

推荐：

```text
Deployment: nanobot-bucket-0
Service:    nanobot-bucket-0
Deployment: nanobot-bucket-1
Service:    nanobot-bucket-1
```

### 12.2 Deployment 模板示例

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nanobot-bucket-0
  namespace: nanobot
  labels:
    app: nanobot-bucket
    bucket-id: bucket-0
spec:
  replicas: 1
  selector:
    matchLabels:
      app: nanobot-bucket
      bucket-id: bucket-0
  template:
    metadata:
      labels:
        app: nanobot-bucket
        bucket-id: bucket-0
    spec:
      containers:
        - name: bucket
          image: nanobot-bucket:latest
          ports:
            - containerPort: 8080
          env:
            - name: BUCKET_ID
              value: bucket-0
            - name: WORKSPACE_ROOT
              value: /app/nanobot_workspaces
          volumeMounts:
            - name: workspaces
              mountPath: /app/nanobot_workspaces
      volumes:
        - name: workspaces
          hostPath:
            path: /opt/nanobot/workspaces
            type: DirectoryOrCreate
```

### 12.3 Service 模板示例

```yaml
apiVersion: v1
kind: Service
metadata:
  name: nanobot-bucket-0
  namespace: nanobot
  labels:
    app: nanobot-bucket
    bucket-id: bucket-0
spec:
  selector:
    app: nanobot-bucket
    bucket-id: bucket-0
  ports:
    - port: 8080
      targetPort: 8080
```

---

## 13. RBAC 权限修改

`container-up` 需要有权限创建和管理 bucket 对应的 Deployment、Service、Pod。

建议增加或修改 `k8s/rbac.yaml`。

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: container-up
  namespace: nanobot
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: container-up-bucket-manager
  namespace: nanobot
rules:
  - apiGroups: ["apps"]
    resources: ["deployments", "deployments/scale"]
    verbs: ["get", "list", "watch", "create", "patch", "update", "delete"]
  - apiGroups: [""]
    resources: ["pods", "services"]
    verbs: ["get", "list", "watch", "create", "patch", "update", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: container-up-bucket-manager
  namespace: nanobot
subjects:
  - kind: ServiceAccount
    name: container-up
    namespace: nanobot
roleRef:
  kind: Role
  name: container-up-bucket-manager
  apiGroup: rbac.authorization.k8s.io
```

`container-up` 的 Deployment 中需要增加：

```yaml
spec:
  template:
    spec:
      serviceAccountName: container-up
```

---

## 14. container-up 接口改造建议

当前 `container-up` 中大概率已有创建用户实例、销毁用户实例、订阅 frontend 等接口。建议将原有“直接创建容器/实例”的逻辑改为先调用 `BucketScheduler`。

### 14.1 创建或获取用户实例接口

原逻辑可能类似：

```python
@app.post("/subscribe/{frontend_id}")
async def subscribe_frontend(frontend_id: str, sub_form: SubForm):
    ...
    create_user_container(...)
```

建议改为：

```python
@app.post("/subscribe/{frontend_id}")
async def subscribe_frontend(frontend_id: str, sub_form: SubForm):
    runtime = bucket_scheduler.get_or_create_user_instance(
        user_id=sub_form.user_id,
        frontend_id=frontend_id,
        app_id=getattr(sub_form, "app_id", None),
    )

    return {
        "user_id": runtime.user_id,
        "workspace_path": runtime.workspace_path,
        "bucket_id": runtime.bucket_id,
        "bucket_url": runtime.bucket_url,
        "instance_id": runtime.instance_id,
    }
```

后续消息转发时：

```text
user_id -> user_instances 表 -> bucket_id -> bucket_url -> 调用实例
```

---

## 15. bucket 内部用户实例接口

如果 bucket 容器内目前还没有“创建用户实例”的接口，需要增加。

### 15.1 创建实例

```http
POST /instances
Content-Type: application/json
```

请求体：

```json
{
  "user_id": "u001",
  "instance_id": "u001",
  "workspace_path": "/app/nanobot_workspaces/u001",
  "frontend_id": "qxt-main",
  "app_id": "default"
}
```

返回：

```json
{
  "instance_id": "u001",
  "status": "online"
}
```

### 15.2 销毁实例

```http
DELETE /instances/{instance_id}
```

返回：

```json
{
  "instance_id": "u001",
  "status": "destroyed"
}
```

### 15.3 查询实例

```http
GET /instances/{instance_id}
```

返回：

```json
{
  "instance_id": "u001",
  "status": "online",
  "workspace_path": "/app/nanobot_workspaces/u001"
}
```

---

## 16. 消息转发逻辑改造

消息转发时不要根据用户重新创建实例，应先查询数据库。

流程：

```text
收到用户消息
  ↓
查询 user_instances
  ↓
如果 online 且 bucket_id 存在
      根据 bucket_id 获取 bucket_url
      转发到 bucket 内的 instance_id
  否则
      调用 get_or_create_user_instance
      创建或恢复实例
      再转发消息
```

伪代码：

```python
def route_user_message(user_id: str, message: dict):
    instance = db.get_user_instance(user_id)

    if not instance or instance.status != "online" or not instance.bucket_id:
        runtime = bucket_scheduler.get_or_create_user_instance(
            user_id=user_id,
            frontend_id=message.get("frontend_id"),
            app_id=message.get("app_id"),
        )
    else:
        bucket = db.get_bucket(instance.bucket_id)
        runtime = UserInstanceRuntime(
            user_id=user_id,
            workspace_path=instance.workspace_path,
            bucket_id=instance.bucket_id,
            bucket_url=bucket.service_host,
            instance_id=instance.instance_id,
        )

    return bucket_client.forward_message(
        bucket_url=runtime.bucket_url,
        instance_id=runtime.instance_id,
        message=message,
    )
```

---

## 17. 需要重点避免的问题

### 17.1 不要把用户和 bucket 永久绑定

错误做法：

```text
user_id -> bucket_id 永久固定
```

正确做法：

```text
user_id -> workspace_path 稳定绑定
user_id -> bucket_id 仅在线期间绑定
```

### 17.2 不要销毁用户工作路径

空闲销毁时只销毁运行实例，不删除 workspace。

```text
保留：workspace_path
清除：bucket_id、instance_id、online 状态
```

### 17.3 不要只查 Kubernetes 判断容量

容量判断必须来自数据库。

### 17.4 不要无事务更新 bucket 数量

实例分配和 `current_instances + 1` 必须在同一个事务内完成。

### 17.5 K8s 创建失败要回滚数据库

如果数据库已经占用了 bucket 名额，但 Kubernetes 创建 bucket 或 bucket 内创建用户实例失败，必须释放占用。

---

## 18. 推荐落地步骤

### 阶段一：数据库改造

1. 新增 `user_instances` 表。
2. 新增 `buckets` 表。
3. 增加数据库初始化脚本。
4. 增加基本 CRUD 方法。

完成后应支持：

```python
db.get_user_instance(user_id)
db.upsert_user_instance(...)
db.get_available_bucket()
db.create_next_bucket_record(...)
db.increment_bucket_instances(bucket_id)
db.decrement_bucket_instances(bucket_id)
```

### 阶段二：工作空间逻辑抽离

1. 新增或改造 `WorkspaceManager`。
2. 实现 `get_or_create_workspace(user_id)`。
3. 确保用户实例销毁后不删除 workspace。

### 阶段三：bucket 调度器实现

1. 新增 `BucketScheduler`。
2. 实现 `get_or_create_user_instance()`。
3. 实现 `release_user_instance()`。
4. 加入事务锁。
5. 加入异常回滚逻辑。

### 阶段四：Kubernetes 动态 bucket 管理

1. 新增 `BucketManager`。
2. 实现创建 Deployment。
3. 实现创建 Service。
4. 实现等待 Ready。
5. 增加 RBAC 权限。
6. container-up 使用 `serviceAccountName: container-up`。

### 阶段五：接口接入

1. 修改用户订阅/初始化接口。
2. 修改消息转发接口。
3. 修改实例销毁接口。
4. 所有入口统一调用 `BucketScheduler`，不要绕过调度器直接创建实例。

### 阶段六：空闲回收

1. 记录 `last_active_at`。
2. 定期扫描空闲用户实例。
3. 调用 bucket 内部接口销毁用户实例。
4. 更新用户实例状态为 `destroyed`。
5. 清除 `bucket_id`。
6. 更新 bucket 实例数量。
7. bucket 空闲超过 TTL 后缩容或删除。

---

## 19. 验收测试建议

### 19.1 首次用户进入

测试条件：

```text
数据库中没有 user-a
没有任何 bucket
```

预期结果：

```text
创建 workspace_path
创建 bucket-0
在 bucket-0 中创建 user-a 实例
user_instances.status = online
user_instances.bucket_id = bucket-0
buckets.current_instances = 1
```

### 19.2 在线用户再次进入

测试条件：

```text
user-a status = online
bucket_id = bucket-0
```

预期结果：

```text
不创建新 workspace
不创建新 bucket
直接路由到 bucket-0
```

### 19.3 bucket 未满时新用户进入

测试条件：

```text
bucket-0 current_instances = 5
max_instances = 20
```

预期结果：

```text
user-b 分配到 bucket-0
bucket-0 current_instances = 6
```

### 19.4 bucket 已满时新用户进入

测试条件：

```text
bucket-0 current_instances = 20
max_instances = 20
```

预期结果：

```text
创建 bucket-1
user-new 分配到 bucket-1
bucket-1 current_instances = 1
```

### 19.5 用户空闲销毁

测试条件：

```text
user-a online
bucket_id = bucket-0
```

预期结果：

```text
bucket 内 user-a 实例销毁
user_instances.status = destroyed
user_instances.bucket_id = NULL
workspace_path 保留
bucket-0 current_instances - 1
```

### 19.6 销毁用户重新进入

测试条件：

```text
user-a status = destroyed
workspace_path 已存在
bucket_id = NULL
```

预期结果：

```text
复用原 workspace_path
重新选择一个可用 bucket
创建新的 user-a 实例
status 更新为 online
bucket_id 更新为新 bucket
```

### 19.7 并发创建测试

测试条件：

```text
bucket-0 current_instances = 19
max_instances = 20
同时两个新用户进入
```

预期结果：

```text
只有一个用户进入 bucket-0
另一个用户触发创建 bucket-1
不会出现 bucket-0 current_instances = 21
```

---

## 20. 最终目标架构

改造后的逻辑应为：

```text
用户
  ↓
container-up
  ↓
UserInstance 表
  ├── online → 根据 bucket_id 路由
  └── destroyed / missing
        ↓
      WorkspaceManager 获取 workspace_path
        ↓
      BucketScheduler 选择 bucket
        ↓
      BucketManager 必要时创建 bucket
        ↓
      bucket 内创建用户实例
        ↓
      更新数据库
```

最终形成三层关系：

```text
稳定关系：
user_id -> workspace_path

运行关系：
user_id -> bucket_id -> bucket_url

容量关系：
bucket_id -> current_instances / max_instances
```

---

## 21. 改造结论

本次改造的重点不是“用户应该固定在哪个 bucket”，而是：

```text
用户只关心自己的 workspace
bucket 只是运行资源池
container-up 负责调度
database 负责记录运行态
Kubernetes 负责按需创建承载资源
```

推荐最终实现为：

```text
1. 用户工作空间长期保留
2. 用户实例在线时记录 bucket_id
3. 用户实例销毁时清除 bucket_id
4. bucket 按容量阈值动态复用或创建
5. bucket 空闲后延迟回收
```
