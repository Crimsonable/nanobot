# K8s Branch Layout

这个分支已经按 K8s 分桶运行方案做过裁剪，只保留当前实现真正需要的代码和配置。

## 保留的核心目录

- `container_up/`
  - 统一网关入口，默认单副本部署。
  - 负责 `frontend_id + user_id` 绑定、分桶转发、订阅接入、出站代理，以及 IM/frontend 配置解析。
- `bucket_runtime/`
  - StatefulSet Pod 内的桶运行时，负责用户实例启动、workspace 初始化、空闲回收。
- `nanobot/`
  - Nanobot 主体运行时代码，不再承载 bucket runtime 的本地 relay/进程组管理逻辑。
- `k8s/`
  - 基础部署 YAML 与 `kind` 本地开发配置。
- `workspace/config.json`
  - Nanobot 主配置。
- `workspace/frontends.json`
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

- `workspace/config.json`
  - Nanobot 运行配置。
- `workspace/frontends.json`
  - 前端定义。
  - K8s 中通过 `FRONTENDS_CONFIG_PATH=/mnt/nanobot/frontends/frontends.json` 注入。
  - 每个 frontend 通过 `common_root` 对应自己的公共目录。

## K8s 挂载约定

- `frontends/frontends.json`
  - 对应 `workspace/frontends.json`
- `route-db/container_up.db`
  - `container_up` 的用户到 bucket 持久化路由数据库
- `common/<frontend-id>/config.json`
  - 该 frontend 的公共 Nanobot 配置
- `common/<frontend-id>/skills/`
  - 该 frontend 的公共 skills
- `common/<frontend-id>/templates/`
  - 该 frontend 的公共模板
- `workspaces/`
  - 用户持久化工作目录

## 本地开发建议

1. 修改 `workspace/config.json` 与 `workspace/frontends.json`。
2. 将 `workspace/frontends.json` 同步到 K8s 的 `frontends/frontends.json`，并准备每个 frontend 的 `common_root` 目录。
3. 使用 `k8s/dev-kind/` 做本地验证。

## 说明

这次裁剪的目标不是把仓库变成最小 demo，而是去掉已经不再参与 K8s 方案的旧容器编排实现，保留当前分支可运行、可维护、可部署的最小主干。
