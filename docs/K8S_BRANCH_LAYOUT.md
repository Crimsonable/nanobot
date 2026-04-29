# K8s Branch Layout

这个分支已经按 K8s 分桶运行方案做过裁剪，只保留当前实现真正需要的代码和配置。

## 保留的核心目录

- `container_up/`
  - 统一网关入口，默认单副本部署。
  - 负责用户实例调度、bucket 动态创建/缩容、订阅接入、出站代理，以及 IM/frontend 配置解析。
- `bucket_runtime/`
  - bucket Pod 内的桶运行时，负责用户实例启动、workspace 初始化、实例空闲回收。
- `nanobot/`
  - Nanobot 主体运行时代码，不再承载 bucket runtime 的本地 relay/进程组管理逻辑。
- `k8s/`
  - 基础部署 YAML 与 `kind` 本地开发配置。
- `common/<frontend-id>/config.json`
  - 每个 frontend 的 Nanobot 主配置。
- `common/frontends.json`
  - 前端入口配置，供 `container_up` 和 `bucket_runtime` 共用。

## 已删除的旧实现

- 旧的 `container_up` Docker 编排链路：
  - 动态子容器创建
  - bridge hub / bridge protocol
  - 旧的组织容器调度入口
  - 旧的 `router_service`
  - 旧的 `org_router`
- 旧的 Docker bridge 构建与启动文件：
  - `Dockerfile.bridge`
  - `Dockerfile.container_up`
  - `docker-compose.container-up.yml`
  - `start_org_router.sh`
  - `start_nanobot_bridge.sh`
- 与上述旧链路绑定的测试和示例配置。

## 当前必须保留的配置文件

- `common/<frontend-id>/config.json`
  - Nanobot 运行配置。
- `common/frontends.json`
  - 前端定义。
  - 当前实现固定读取 `BUCKET_MOUNT_ROOT/common/frontends.json`。
  - 每个 frontend 的公共目录固定推导为 `common/<frontend-id>/`。

## K8s 挂载约定

- `common/frontends.json`
  - frontend 注册总表
- `route-db/container_up.db`
  - `container_up` 的 `user_instances` / `buckets` 持久化运行态数据库
- `common/<frontend-id>/config.json`
  - 该 frontend 的公共 Nanobot 配置
- `common/<frontend-id>/skills/`
  - 该 frontend 的公共 skills
- `common/<frontend-id>/templates/`
  - 该 frontend 的公共模板
- `workspaces/`
  - 用户持久化工作目录

## 本地开发建议

1. 修改 `common/frontends.json` 与对应 frontend 的 `common/<frontend-id>/config.json`。
2. 准备每个 frontend 的 `common/<frontend-id>/skills/` 和 `common/<frontend-id>/templates/` 目录。
3. 使用 `k8s/dev-kind/` 做本地验证。

## 说明

这次裁剪的目标不是把仓库变成最小 demo，而是去掉已经不再参与 K8s 方案的旧容器编排实现，保留当前分支可运行、可维护、可部署的最小主干。
