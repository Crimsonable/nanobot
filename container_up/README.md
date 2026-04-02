# container_up

`container_up` 是整个架构的顶层 client 入口。

它负责：

- 接收用户请求：`org_id`、`conversation_id`、`user_id`/`usr_id`、`content`
- 用 SQLite 记录 `org_id -> child container`
- 如果组织容器已存在且可用，就通过内置的 FastAPI WebSocket bridge hub 把消息分发给该 child
- 如果不存在，就启动新的 `nanobot-bridge` 容器，等待 child 的 `org_router` 注册到 hub 后再分发

## API

- `POST /api/message`
- `POST /api/cancel`
- `POST /api/bridge/outbound`
- `POST /subscribe`
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
    "content": "请总结一下这个项目结构",
    "attachments": [],
    "metadata": {
      "source": "client-demo"
    }
  }'
```

`POST /api/message` 现在只负责把消息投递进 child，成功时返回 `accepted` 风格结果，不再同步等待最终回复。

取消请求：

```bash
curl -X POST http://127.0.0.1:8080/api/cancel \
  -H 'Content-Type: application/json' \
  -d '{
    "org_id": "org-1",
    "conversation_id": "conv-1",
    "usr_id": "user-1"
  }'
```

主动外发入口：

```bash
curl -X POST http://127.0.0.1:8080/api/bridge/outbound \
  -H 'Content-Type: application/json' \
  -H 'X-Bridge-Token: <child-bridge-token>' \
  -d '{
    "org_id": "org-1",
    "to": "user-1:::conv-1",
    "content": "后台任务完成",
    "attachments": [],
    "metadata": {
      "source": "bridge"
    }
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

订阅回调：

```bash
curl -X POST http://127.0.0.1:8080/subscribe \
  -H 'Content-Type: application/json' \
  -d '{
    "msgSignature": "...",
    "timeStamp": "1491805325",
    "nonce": "KkSvMbDM",
    "encrypt": "..."
  }'
```

## 工作目录结构

现在使用单一工作根目录 `HOST_WORKSPACE_ROOT`。

- 宿主机公共 config 挂载到组织容器：
  - `HOST_SHARED_CONFIG -> /app/nanobot_workspaces/config.json`
- 宿主机组织工作目录挂载到组织容器：
  - `HOST_WORKSPACE_ROOT/<org_id> -> /app/nanobot_workspaces`
- 子容器里 `org_router` 会按用户直接使用：
  - 公共 config：`/app/nanobot_workspaces/config.json`
  - 实例 workspace：`/app/nanobot_workspaces/<safe-user-id>-<hash>/`

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

3. 组织容器调度：
- `CHILD_IMAGE`
- `CHILD_NETWORK`
- `CHILD_BRIDGE_TOKEN`
- `CHILD_READY_TIMEOUT`
- `IDLE_TIMEOUT_SECONDS`
- `CLEANUP_SCAN_INTERVAL`
- `INSTANCE_IDLE_TIMEOUT_SECONDS`

4. 订阅回调与消息回传：
- `APP_ID`
- `APP_SECRET`
- `CORP_ID`
- `CALLBACK_TOKEN`
- `ACCESS_URL`
- `SEND_MSG_URL`
- `SEND_MSG_TIMEOUT`
- `SEND_MSG_RETRY_COUNT`
- `SEND_MSG_RETRY_BACKOFF`

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

- `CHILD_IMAGE`
  - 动态拉起的组织容器镜像
- `CHILD_NETWORK`
  - 组织容器加入的 Docker 网络
- `CHILD_BRIDGE_TOKEN`
  - `container_up` 和组织容器之间的 websocket bridge 鉴权 token
- `CHILD_READY_TIMEOUT`
  - 等组织容器注册到 bridge hub 的超时时间
- `IDLE_TIMEOUT_SECONDS`
  - 组织容器空闲多久后被回收
- `CLEANUP_SCAN_INTERVAL`
  - 回收线程的扫描周期
- `INSTANCE_IDLE_TIMEOUT_SECONDS`
  - 组织容器内用户实例空闲多久后被 `org_router` 回收
- `APP_ID`
  - 订阅解密和 access token 获取使用的应用 ID
- `APP_SECRET`
  - 订阅解密使用的应用密钥
- `CORP_ID`
  - 企业或组织 ID
- `CALLBACK_TOKEN`
  - 订阅回调签名校验 token
- `ACCESS_URL`
  - 获取 access token 的接口地址
- `SEND_MSG_URL`
  - 将处理结果回发到上游 IM 的接口地址
- `SEND_MSG_TIMEOUT`
  - 回发消息请求超时时间
- `SEND_MSG_RETRY_COUNT`
  - 回发消息失败后的最大重试次数
- `SEND_MSG_RETRY_BACKOFF`
  - 回发消息失败后的退避基数秒数

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
- 用 `CHILD_IMAGE`、`CHILD_NETWORK`、`CHILD_BRIDGE_TOKEN` 拉起组织容器
- 在 `CHILD_READY_TIMEOUT` 内等待组织容器注册成功

3. 组织容器启动 `org_router`
- `org_router` 固定回连 `ws://container-up:<CONTAINER_UP_PORT>/ws/bridge`
- 用 `INSTANCE_IDLE_TIMEOUT_SECONDS` 管理用户实例生命周期

4. `org_router` 收到 parent 转发的消息
- 按 `user_id` 定位用户实例 workspace 目录
- 若实例不存在，则创建：
  - `/app/nanobot_workspaces/<safe-user-id>-<hash>/`
- 实例直接复用：
  - 公共 config `/app/nanobot_workspaces/config.json`

5. `org_router` 拉起用户实例
- 用户实例运行 `nanobot.local_service`
- 该实例只服务一个用户自己的 workspace
- 对话历史由 `conversation_id` 映射到该用户 workspace 下的 session / history
- 用户自己新增的 skills 保存在该实例自己的 `skills/`

6. 用户实例开始处理请求
- `org_router` 把统一的 bridge 消息包转发给本地实例
- 本地实例把消息直接 `publish_inbound` 到 nanobot bus
- `AgentLoop` 常驻从 bus 消费，同一 session 的串行由 nanobot 自己保证
- 任意出站消息统一走 `bus.outbound -> outbound_message`
- `container_up` 收到 `outbound_message` 后按 `chat_id + metadata` 直接发到上游 IM

## 统一消息格式

bridge 链路现在统一使用和 nanobot channel 一致的消息字段：

- inbound 到 child：
  - `type: "inbound_message"`
  - `channel`
  - `sender_id`
  - `chat_id`
  - `session_key`
  - `content`
  - `attachments`
  - `metadata`
- outbound 回 parent：
  - `type: "outbound_message"`
  - `channel`
  - `chat_id`
  - `content`
  - `attachments`
  - `metadata`
  - `reply_to`（可选）

其中 `chat_id` 对 bridge 统一使用复合格式：

- `<sender_id>:::<conversation_id>`

这样 parent 侧既能保留原始来源标识，也能在需要时解析出具体会话。

## 部署注意

这个服务需要控制宿主机 Docker，所以必须挂载：

```text
/var/run/docker.sock:/var/run/docker.sock
```

同时，`HOST_WORKSPACE_ROOT` 和 `HOST_SHARED_CONFIG` 都必须是宿主机绝对路径，并且挂载到 `container_up` 容器内相同路径。这样 `container_up` 才能把这些路径原样传给宿主机 Docker daemon 创建子容器。

## 网络模型

当前实现只支持 Docker bridge 网络：

- `container_up` 固定加入 `nanobot-stack`
- child 固定加入 `nanobot-stack`
- child 固定回连 `ws://container-up:<CONTAINER_UP_PORT>/ws/bridge`

不再兼容 `host` 模式，也不再暴露 `PARENT_BRIDGE_URL` 作为外部配置项。

对于 bridge 渠道的主动回发：

- 正常情况下，child 直接通过与 parent 的长连接 websocket 回传 `outbound_message`
- 若 bridge channel 自己独立运行且 websocket 不可用，仍可用 `/api/bridge/outbound` 作为兜底
- 共享 `config.json` 中的 `channels.bridge` 只需要保留：
  - `enabled`
  - `bridgeToken`
  - `allowFrom`

## Compose

根目录提供了 [docker-compose.container-up.yml](/mnt/d/codes/nanobot_modify/nanobot/docker-compose.container-up.yml) 和 [.env](/mnt/d/codes/nanobot_modify/nanobot/.env)。

当前这份 [.env](/mnt/d/codes/nanobot_modify/nanobot/.env) 已经配置为：

- 工作根目录：
  - `/mnt/d/codes/nanobot_modify/nanobot/container_up_runtime/workspaces`
- 公共 config 模板：
  - [workspace/config.json](/mnt/d/codes/nanobot_modify/nanobot/workspace/config.json)
- SQLite 数据目录：
  - `/mnt/d/codes/nanobot_modify/nanobot/container_up_runtime/data`
- 子容器镜像：
  - `nanobot-bridge:latest`
- 子容器网络：
  - `nanobot-stack`
- bridge token：
  - `my-shared-token`
- 组织容器 idle timeout：
  - `3600`
- 用户实例 idle timeout：
  - `1800`
- cleanup scan interval：
  - `300`

当前实现里，`/api/message` 的最小业务字段只有：

- `org_id`
- `conversation_id`
- `user_id`/`usr_id`
- `content`

其中：

- `org_id` 决定组织容器
- `user_id` 决定组织容器内复用哪个本地用户实例
- `conversation_id` 决定 session key 和会话上下文

`tenant_id` 已经不再参与这条链路。

另外，`container_up` 的配置项已经统一收口到：

- [container_up/settings.py](/mnt/d/codes/nanobot_modify/nanobot/container_up/settings.py)

数据库和 bridge / 调度逻辑也已拆分到：

- [container_up/db_store.py](/mnt/d/codes/nanobot_modify/nanobot/container_up/db_store.py)
- [container_up/bridge_state.py](/mnt/d/codes/nanobot_modify/nanobot/container_up/bridge_state.py)
- [container_up/router_service.py](/mnt/d/codes/nanobot_modify/nanobot/container_up/router_service.py)

启动前建议先确认：

- `nanobot-bridge:latest` 已经构建完成
- [workspace/config.json](/mnt/d/codes/nanobot_modify/nanobot/workspace/config.json) 中的模型配置是你想要的值

启动：

```bash
docker compose -f docker-compose.container-up.yml up --build
```
