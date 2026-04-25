# Project Architecture

## Overview

这个项目当前采用统一网关 + K8s bucket runtime 的结构：

1. `container_up`
   - 统一对外入口。
   - 默认单副本部署，横向扩容对象是 bucket，而不是网关。
   - 接收订阅事件、调试请求和标准入站请求。
   - 根据 `frontend_id + user_id` 维护 bucket 绑定。
   - bucket 数量以 `BUCKET_COUNT` 为准；StatefulSet `replicas` 必须与其保持一致。
   - 将请求转发到固定的 bucket pod。
   - 负责统一出站发送。

2. `bucket_runtime`
   - 运行在 StatefulSet Pod 内。
   - 一个 Pod 对应一个 bucket。
   - 在 bucket 内按用户启动和复用独立的 Nanobot 实例。
   - 管理 workspace 初始化、空闲回收、端口分配和实例 relay。

3. `nanobot`
   - 作为用户级 AI agent/gateway 本体存在。
   - 不再承载 bucket runtime 的本地 relay、进程组和编排逻辑。

## Request Flow

1. 外部渠道请求进入 `container_up`。
2. 网关解析 `frontend_id` 和 `user_id`。
3. 若没有绑定，则选择 bucket 并持久化绑定。
4. 网关转发到目标 bucket pod。
5. `bucket_runtime` 为该用户启动或复用实例。
6. 用户实例处理后，通过 bucket runtime 回调统一网关的 `/outbound`。
7. 网关根据 frontend 配置发送到对应外部渠道。

## Frontend Public Layout

`frontends.json` 只有一份，是全局总表，单独放在 frontend 公共目录之外。

每个 frontend 只保留自己的公共目录：

```text
frontends/
  frontends.json
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

`workspace/frontends.json` 里的每个 frontend 至少应定义：

- `id`
- `provider`
- `common_root`

系统会从 `common_root` 自动推导：

- `config_path = common_root/config.json`
- `builtin_skills_dir = common_root/skills`
- `template_dir = common_root/templates`

如果需要，也可以对这些路径单独覆盖。

## Key Directories

- `container_up/`
  - 统一网关与 frontend/IM 相关代码。
- `bucket_runtime/`
  - bucket 内部进程管理与 relay 逻辑。
- `nanobot/`
  - Nanobot 主体。
- `k8s/`
  - K8s 资源清单。
- `workspace/`
  - 本地配置模板。
- `docs/`
  - 架构与部署文档。

## K8s Resources

- `container-up` Deployment + Service
  - 统一网关
- `nanobot-bucket` StatefulSet + Headless Service
  - 固定 bucket pod
- `nanobot-source` PV/PVC
  - 公共源码
- `nanobot-common` PV/PVC
  - frontend 公共目录
- `nanobot-frontends` PV/PVC
  - 全局 frontends registry
- `nanobot-route-db` PV/PVC
  - `frontend_id + user_id -> bucket_id` 持久化路由库
- `nanobot-workspaces` PV/PVC
  - 用户持久化 workspace

## Runtime Boundaries

下面这些能力现在属于 `bucket_runtime`，不应该再放在 `nanobot/` 中：

- 本地 websocket relay
- 子进程组关闭与信号处理
- bucket 内用户实例的进程编排
- bucket 级端口分配
- bucket 级 idle reaper

## Configuration

主要环境变量：

- `FRONTENDS_CONFIG_PATH`
  - frontend registry 路径
- `CONTAINER_UP_DB_PATH`
  - 路由绑定数据库文件路径
- `HOST_WORKSPACE_ROOT`
  - 用户 workspace 根目录
- `BUCKET_COUNT`
  - bucket 数量主配置
- `BUCKET_SERVICE_NAME`
  - bucket service 名称
- `BUCKET_STATEFULSET_NAME`
  - bucket StatefulSet 名称
- `OUTBOUND_GATEWAY_URL`
  - bucket runtime 回调统一网关的地址

## Current Entry Points

- 统一网关：
  - `python -m container_up.app`
- bucket runtime：
  - `python -m bucket_runtime.main`
- bucket 内本地 relay：
  - `python -m bucket_runtime.local_service`
