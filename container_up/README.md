# container_up

`container_up` 是整个架构的顶层 client 入口。

它负责：

- 接收用户请求：`session_id`、`user_id`/`usr_id`、`content`
- 用 SQLite 记录 `session_id -> child container`
- 如果会话容器已存在且可用，就通过内置的 FastAPI WebSocket bridge hub 把请求分发给该 child
- 如果不存在，就启动新的 `nanobot-bridge` 容器，等待 child 的 `BridgeChannel` 注册到 hub 后再分发

## API

- `POST /api/message`
- `POST /api/cancel`
- `GET /api/session/{session_id}`
- `GET /healthz`

`POST /api/message` 最小请求示例：

```json
{
  "session_id": "demo-session",
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
    "session_id": "demo-session",
    "usr_id": "user-1",
    "content": "hello"
  }'
```

如果要显式传更多字段：

```bash
curl -X POST http://127.0.0.1:8080/api/message \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "demo-session",
    "user_id": "user-1",
    "tenant_id": "default",
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
    "session_id": "demo-session",
    "usr_id": "user-1",
    "request_id": "req-1"
  }'
```

查看某个 session 当前路由到了哪个容器：

```bash
curl http://127.0.0.1:8080/api/session/demo-session
```

健康检查：

```bash
curl http://127.0.0.1:8080/healthz
```

## 工作目录结构

现在使用单一工作根目录 `HOST_WORKSPACE_ROOT`。

- 默认配置模板文件位于：
  - `HOST_WORKSPACE_ROOT/config.json`
- 每个 session 的实际挂载目录位于：
  - `HOST_WORKSPACE_ROOT/<session_id>`
- 每个子容器最终挂载的是：
  - `HOST_WORKSPACE_ROOT/<session_id> -> /app/workspace`
- 子容器中 `nanobot` 实际运行时使用的 workspace 也固定为：
  - `/app/workspace`

如果 `HOST_WORKSPACE_ROOT/config.json` 不存在，`container_up` 会在启动时从 `SOURCE_TEMPLATE_CONFIG` 复制一份过来。

## 子容器端口策略

`container_up` 创建出来的 `nanobot-bridge` 子容器，当前只需要暴露 `nanobot gateway` 端口。

- 子容器内部 gateway 端口固定为 `CHILD_GATEWAY_PORT`，默认 `18790`
- 子容器不会再起本地 `bridge_service`
- child 内的 `BridgeChannel` 会主动连接 `container_up` 的 `WS /ws/bridge`
- `container_up` 以 `session_id -> child websocket` 路由请求

也就是说，当前是：

- parent WebSocket 固定
- 容器名动态
- 路由靠 `session_id -> websocket`

## 环境变量

关键环境变量分四类：

1. `container_up` 自身：
- `CONTAINER_UP_PORT`
- `CONTAINER_UP_DB_PATH`

2. 工作目录：
- `HOST_WORKSPACE_ROOT`
- `SOURCE_TEMPLATE_CONFIG`

3. 子容器运行：
- `CHILD_IMAGE`
- `CHILD_NETWORK`
- `CHILD_GATEWAY_PORT`
- `CHILD_BRIDGE_TOKEN`
- `PARENT_BRIDGE_URL`
- `IDLE_TIMEOUT_SECONDS`
- `CLEANUP_SCAN_INTERVAL`

4. 模型配置：
- `MODEL_PROVIDER`
- `MODEL_API_KEY`
- `MODEL_API_BASE`
- `MODEL_NAME`

5. skill 挂载：
- `HOST_SKILLS_SOURCE`
- `CHILD_SKILLS_DIR`

## 配置同步机制

当你修改 `container_up` 的环境变量并重启 `container_up` 时，它会在启动阶段自动做一次 reconcile：

1. 重新生成根模板配置：
- `HOST_WORKSPACE_ROOT/config.json`

2. 遍历数据库中记录的所有 session：
- 把新的 bridge/model 配置同步写入每个 `HOST_WORKSPACE_ROOT/<session_id>/config.json`

3. 如果某个 session 对应的子容器正在运行，且配置发生变化：
- 自动重启该子容器
- 等待 child 重新注册到 `container_up` 的 bridge hub 后继续服务

这意味着：

- 新会话会继承新的配置
- 已存在且正在运行的子容器，也会在 `container_up` 重启时同步到新配置

## 定期清理

`container_up` 启动后会起一个后台清理线程，按固定间隔扫描 SQLite 中记录的 session。

- `CLEANUP_SCAN_INTERVAL`
  - 扫描周期，默认 `300` 秒
- `IDLE_TIMEOUT_SECONDS`
  - idle 超时时间，默认 `3600` 秒

当某个 session 超过 `IDLE_TIMEOUT_SECONDS` 没有新的用户请求时：

- 对应子容器会被删除
- SQLite 中这条 session 路由记录也会被删除
- 但 `HOST_WORKSPACE_ROOT/<session_id>` 不会删除

所以同一个 `session_id` 后续再次连上来时：

- `container_up` 会重新创建容器
- 继续挂载原来的 workspace 目录
- 从而恢复该 session 的卷数据

## 部署注意

这个服务需要控制宿主机 Docker，所以必须挂载：

```text
/var/run/docker.sock:/var/run/docker.sock
```

同时，`HOST_WORKSPACE_ROOT` 和 `SOURCE_TEMPLATE_CONFIG` 都必须是宿主机绝对路径，并且挂载到容器内相同路径。这样 `container_up` 才能把这些路径原样传给宿主机 Docker daemon 创建子容器。

## Compose

根目录提供了 [docker-compose.container-up.yml](/mnt/d/codes/nanobot_modify/nanobot/docker-compose.container-up.yml) 和 [.env](/mnt/d/codes/nanobot_modify/nanobot/.env)。

当前这份 [.env](/mnt/d/codes/nanobot_modify/nanobot/.env) 已经配置为：

- 工作根目录：
  - `/mnt/d/codes/nanobot_modify/nanobot/container_up_runtime/workspaces`
- 根模板来源：
  - [workspace/config.json](/mnt/d/codes/nanobot_modify/nanobot/workspace/config.json)
- SQLite 数据目录：
  - `/mnt/d/codes/nanobot_modify/nanobot/container_up_runtime/data`
- 子容器镜像：
  - `nanobot-bridge:latest`
- bridge token：
  - `my-shared-token`
- idle timeout：
  - `3600`
- cleanup scan interval：
  - `300`
- 模型 provider：
  - `vllm`
- skill 源目录：
  - `/mnt/d/codes/nanobot_modify/nanobot/nanobot/skills`
- 子容器 skill 挂载目录：
  - `/app/workspace/skills`

启动前建议先确认：

- `nanobot-bridge:latest` 已经构建完成
- `.env` 中的模型配置是你想要的值

启动：

```bash
docker compose -f docker-compose.container-up.yml up --build
```
