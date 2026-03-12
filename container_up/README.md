# container_up

`container_up` 是整个架构的顶层 client 入口。

它负责：

- 接收用户请求：`org_id`、`conversation_id`、`user_id`/`usr_id`、`content`
- 用 SQLite 记录 `org_id -> child container`
- 如果组织容器已存在且可用，就通过内置的 FastAPI WebSocket bridge hub 把请求分发给该 child
- 如果不存在，就启动新的 `nanobot-bridge` 容器，等待 child 的 `org_router` 注册到 hub 后再分发

## API

- `POST /api/message`
- `POST /api/cancel`
- `GET /api/org/{org_id}`
- `GET /healthz`

`POST /api/message` 最小请求示例：

```json
{
  "org_id": "org-1",
  "conversation_id": "conv-1",
  "usr_id": "user-1",
  "content": "hello"
}
```

## 调用方式

默认启动后，`container_up` 对外监听：

```text
http://127.0.0.1:8080
```

发送消息：

```bash
curl -X POST http://127.0.0.1:8080/api/message \
  -H 'Content-Type: application/json' \
  -d '{
    "org_id": "org-1",
    "conversation_id": "conv-1",
    "usr_id": "user-1",
    "content": "hello"
  }'
```

如果要显式传更多字段：

```bash
curl -X POST http://127.0.0.1:8080/api/message \
  -H 'Content-Type: application/json' \
  -d '{
    "org_id": "org-1",
    "conversation_id": "conv-1",
    "user_id": "user-1",
    "request_id": "req-1",
    "content": "请总结一下这个项目结构",
    "attachments": [],
    "metadata": {
      "source": "client-demo"
    },
    "timeout_seconds": 300
  }'
```

取消请求：

```bash
curl -X POST http://127.0.0.1:8080/api/cancel \
  -H 'Content-Type: application/json' \
  -d '{
    "org_id": "org-1",
    "conversation_id": "conv-1",
    "usr_id": "user-1",
    "request_id": "req-1"
  }'
```

查看某个组织当前路由到了哪个容器：

```bash
curl http://127.0.0.1:8080/api/org/org-1
```

健康检查：

```bash
curl http://127.0.0.1:8080/healthz
```

## 工作目录结构

现在使用单一工作根目录 `HOST_WORKSPACE_ROOT`。

- 宿主机公共 config 挂载到组织容器：
  - `HOST_SHARED_CONFIG -> /app/nanobot_workspaces/config.json`
- 宿主机组织工作目录挂载到组织容器：
  - `HOST_WORKSPACE_ROOT/<org_id> -> /app/nanobot_workspaces`
- 宿主机公共 skills 挂载到组织容器 builtin skills 目录：
  - `HOST_SHARED_SKILLS -> /app/nanobot/skills`
- 子容器里 `org_router` 会按用户直接使用：
  - 公共 config：`/app/nanobot_workspaces/config.json`
  - 实例 workspace：`/app/nanobot_workspaces/<user_id-hash>/`

## 子容器端口策略

`container_up` 创建出来的 `nanobot-bridge` 子容器，当前默认启动的是 `org_router`。

- `org_router` 主动连接 `container_up` 的 `WS /ws/bridge`
- `container_up` 以 `org_id -> child websocket` 路由请求
- `org_router` 在容器内按 `user_id` 拉起本地 `nanobot.local_service`
- 每个用户实例复用同一个公共 config，但使用独立 workspace

也就是说，当前是：

- parent WebSocket 固定
- 容器名动态
- 外层路由靠 `org_id -> websocket`
- 内层路由靠 `user_id -> local instance`

## 环境变量

关键环境变量分五类：

1. `container_up` 服务自身：
- `CONTAINER_UP_HOST`
- `CONTAINER_UP_PORT`
- `CONTAINER_UP_PUBLIC_PORT`
- `CONTAINER_UP_DB_PATH`
- `HOST_CONTAINER_UP_DATA`

2. 模板和工作目录：
- `HOST_WORKSPACE_ROOT`
- `HOST_SHARED_CONFIG`
- `HOST_SHARED_SKILLS`

3. 组织容器调度：
- `CHILD_IMAGE`
- `CHILD_NETWORK`
- `CHILD_BRIDGE_TOKEN`
- `PARENT_BRIDGE_URL`
- `CHILD_READY_TIMEOUT`
- `FORWARD_TIMEOUT`
- `IDLE_TIMEOUT_SECONDS`
- `CLEANUP_SCAN_INTERVAL`
- `INSTANCE_IDLE_TIMEOUT_SECONDS`

4. 组织级共享 skill 挂载：
- 共享 skills 直接挂载到 child 容器的 builtin skills 路径
- 用户实例自己的 `workspace/skills` 仍然保留

### 配置项含义

- `CONTAINER_UP_HOST`
  - `container_up` 容器内监听地址
- `CONTAINER_UP_PORT`
  - `container_up` 容器内监听端口
- `CONTAINER_UP_PUBLIC_PORT`
  - 宿主机对外暴露端口
- `CONTAINER_UP_DB_PATH`
  - SQLite 路径，记录 `org_id -> child container`
- `HOST_CONTAINER_UP_DATA`
  - 宿主机持久化 `container_up` 数据目录

- `HOST_WORKSPACE_ROOT`
  - 宿主机组织工作根目录
  - 每个组织对应 `HOST_WORKSPACE_ROOT/<org_id>`
- `HOST_SHARED_CONFIG`
  - 宿主机上的公共 config 模板
  - 组织容器内会只读挂载到 `/app/nanobot_workspaces/config.json`
  - 所有用户实例直接复用这个文件
- `HOST_SHARED_SKILLS`
  - 宿主机上的公共 skills 目录
  - 会直接挂到 child 容器内 `nanobot` 的 builtin skills 路径

- `CHILD_IMAGE`
  - 动态拉起的组织容器镜像
- `CHILD_NETWORK`
  - 组织容器加入的 Docker 网络
- `CHILD_BRIDGE_TOKEN`
  - `container_up` 和组织容器之间的 websocket bridge 鉴权 token
- `PARENT_BRIDGE_URL`
  - 组织容器内 `org_router` 回连 parent 的 websocket 地址
- `CHILD_READY_TIMEOUT`
  - 等组织容器注册到 bridge hub 的超时时间
- `FORWARD_TIMEOUT`
  - 单次 `/api/message` 在 parent 层等待最终结果的最长时间
- `IDLE_TIMEOUT_SECONDS`
  - 组织容器空闲多久后被回收
- `CLEANUP_SCAN_INTERVAL`
  - 回收线程的扫描周期
- `INSTANCE_IDLE_TIMEOUT_SECONDS`
  - 组织容器内用户实例空闲多久后被 `org_router` 回收

- 公共 skills 的加载方式
  - 共享 skills 作为 builtin skills 被所有用户实例直接读取
  - 用户实例自己的技能仍写在 `workspace/skills`
  - 因此“公共 skills 更新”与“用户私有 skills 创建”互不影响

## 配置同步机制

当你修改 `container_up` 的环境变量并重启 `container_up` 时，它会在启动阶段自动做一次 reconcile：

1. 读取 `HOST_SHARED_CONFIG`
2. 遍历数据库中记录的所有组织，确认工作目录与容器路由状态
3. 如果某个组织对应的子容器需要重建：
- 重新挂载同一个公共 config 和公共 skills
- 等待 child 重新注册到 `container_up` 的 bridge hub 后继续服务

这意味着：

- 新组织会直接使用最新公共 config
- 已存在且正在运行的组织容器，在重新拉起后也会直接使用最新公共 config
- 已存在的用户实例在下次拉起时会直接读取最新公共 config

## 定期清理

`container_up` 启动后会起一个后台清理线程，按固定间隔扫描 SQLite 中记录的组织容器。

- `CLEANUP_SCAN_INTERVAL`
  - 扫描周期，默认 `300` 秒
- `IDLE_TIMEOUT_SECONDS`
  - idle 超时时间，默认 `3600` 秒

当某个组织超过 `IDLE_TIMEOUT_SECONDS` 没有新的用户请求时：

- 对应子容器会被删除
- SQLite 中这条 org 路由记录也会被删除
- 但 `HOST_WORKSPACE_ROOT/<org_id>` 不会删除

而在组织容器内部：

- `org_router` 会按 `INSTANCE_IDLE_TIMEOUT_SECONDS` 回收空闲用户实例进程
- 但不会删除 `/app/nanobot_workspaces/<user_id-hash>/`

所以同一个 `org_id` 后续再次连上来时：

- `container_up` 会重新创建容器
- 继续挂载原来的 workspace 目录
- 从而恢复该组织的数据

## 调用流程

按配置项串起来，完整调用流程是：

1. client 调用 `POST /api/message`
- 请求里传 `org_id`、`conversation_id`、`user_id`、`content`

2. `container_up` 收到请求
- 用 `HOST_WORKSPACE_ROOT` 找到或创建组织目录 `HOST_WORKSPACE_ROOT/<org_id>`
- 用 `CHILD_IMAGE`、`CHILD_NETWORK`、`CHILD_BRIDGE_TOKEN`、`PARENT_BRIDGE_URL` 拉起组织容器
- 在 `CHILD_READY_TIMEOUT` 内等待组织容器注册成功

3. 组织容器启动 `org_router`
- `org_router` 用 `PARENT_BRIDGE_URL` 回连 `container_up`
- 用 `INSTANCE_IDLE_TIMEOUT_SECONDS` 管理用户实例生命周期

4. `org_router` 收到 parent 转发的消息
- 按 `user_id` 定位用户实例 workspace 目录
- 若实例不存在，则创建：
  - `/app/nanobot_workspaces/<user_id-hash>/`
- 实例直接复用：
  - 公共 config `/app/nanobot_workspaces/config.json`
- 公共 skills 直接通过 builtin skills 挂载对实例生效

5. `org_router` 拉起用户实例
- 用户实例运行 `nanobot.local_service`
- 该实例只服务一个用户自己的 workspace
- 对话历史仍由 `conversation_id` 映射到该用户 workspace 下的历史文件
- 用户自己新增的 skills 保存在该实例自己的 `skills/`

6. 用户实例开始处理请求
- `org_router` 把 `content`、`conversation_id`、`user_id` 转发给本地实例
- 实例进入 `AgentLoop`
- 若请求在 `FORWARD_TIMEOUT` 内完成，结果经 `org_router` 回传给 `container_up`
- `container_up` 再把最终结果返回给 client

## 部署注意

这个服务需要控制宿主机 Docker，所以必须挂载：

```text
/var/run/docker.sock:/var/run/docker.sock
```

同时，`HOST_WORKSPACE_ROOT`、`HOST_SHARED_CONFIG`、`HOST_SHARED_SKILLS` 都必须是宿主机绝对路径，并且挂载到 `container_up` 容器内相同路径。这样 `container_up` 才能把这些路径原样传给宿主机 Docker daemon 创建子容器。

## Compose

根目录提供了 [docker-compose.container-up.yml](/mnt/d/codes/nanobot_modify/nanobot/docker-compose.container-up.yml) 和 [.env](/mnt/d/codes/nanobot_modify/nanobot/.env)。

当前这份 [.env](/mnt/d/codes/nanobot_modify/nanobot/.env) 已经配置为：

- 工作根目录：
  - `/mnt/d/codes/nanobot_modify/nanobot/container_up_runtime/workspaces`
- 公共 config 模板：
  - [workspace/config.json](/mnt/d/codes/nanobot_modify/nanobot/workspace/config.json)
- 公共 builtin skills：
  - [/mnt/d/codes/nanobot_modify/nanobot/nanobot/skills](/mnt/d/codes/nanobot_modify/nanobot/nanobot/skills)
- SQLite 数据目录：
  - `/mnt/d/codes/nanobot_modify/nanobot/container_up_runtime/data`
- 子容器镜像：
  - `nanobot-bridge:latest`
- bridge token：
  - `my-shared-token`
- 组织容器 idle timeout：
  - `3600`
- 用户实例 idle timeout：
  - `1800`
- idle timeout：
  - `300`
- cleanup scan interval：
  - `300`

启动前建议先确认：

- `nanobot-bridge:latest` 已经构建完成
- `.env` 中的模型配置是你想要的值

启动：

```bash
docker compose -f docker-compose.container-up.yml up --build
```
