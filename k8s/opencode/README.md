# OpenCode Kubernetes 部署入口

Channel Gateway 与 CU 部署在同一个单副本 Pod 中，分别运行在 `gateway`（8000）和 `control`（8001）容器。外部 NodePort 30081 指向 Gateway，CU 仅供内部控制连接；分层升级及已有独立 Deployment 的合并步骤见 [渠道分层部署说明](../../docs/CHANNEL_GATEWAY.md)。


完整操作只维护在 [部署与运维手册](../../docs/DEPLOYMENT_MANUAL.md)。首次部署按手册第 1～6 章执行，再跳到第 11 章测试；停服和删除章节仅在维护或卸载时执行。

| 要做的事 | 手册章节 |
| --- | --- |
| 确认环境、机器分工、网络、默认参数 | [1. 环境与部署参数](../../docs/DEPLOYMENT_MANUAL.md#1-环境与部署参数) |
| 配置唯一运行节点和标签 | [2. 节点配置](../../docs/DEPLOYMENT_MANUAL.md#2-节点配置) |
| 确认每个文件放在哪台机器、哪个目录 | [3. 目录与文件位置](../../docs/DEPLOYMENT_MANUAL.md#3-目录与文件位置) |
| 选择本地盘，或设置 NFS 导出、权限、客户端挂载和 PV | [4. 存储配置](../../docs/DEPLOYMENT_MANUAL.md#4-存储配置本地盘或-nfs) |
| 下载依赖、打镜像、传文件、导入镜像、配置模型及 MinIO | [5. 构建镜像与放置文件](../../docs/DEPLOYMENT_MANUAL.md#5-构建镜像与放置文件) |
| 启动 CU 和可选 Web 服务 | [6. 启动服务](../../docs/DEPLOYMENT_MANUAL.md#6-启动服务) |
| 停服但保留数据，之后恢复服务 | [7. 关闭与恢复服务](../../docs/DEPLOYMENT_MANUAL.md#7-关闭与恢复服务) |
| 更新源码、插件、配置和已有 bucket | [8. 更新服务](../../docs/DEPLOYMENT_MANUAL.md#8-更新服务) |
| 删除 namespace、PVC、PV，并保留文件重装 | [9. 删除命名空间与重装](../../docs/DEPLOYMENT_MANUAL.md#9-删除命名空间与重装) |
| 查看实时日志、崩溃日志、挂载和调度问题 | [10. 日志与排障](../../docs/DEPLOYMENT_MANUAL.md#10-日志与排障) |
| 同步、会话、去重、异步、取消和附件测试 | [11. 测试命令](../../docs/DEPLOYMENT_MANUAL.md#11-测试命令) |

## 本目录清单

| 文件 | 用途 |
| --- | --- |
| storage.yaml | namespace、local/NFS PV、四个 PVC；首次绑定前选择存储方案 |
| backend.yaml | CU 的 ServiceAccount/RBAC、Gateway 与 CU 共用的单副本 Deployment、两个 Service |
| web-server.yaml | 可选 Web 代理与异步回调接收器 |
| engineering-scene-mcp.yaml | 可选工程场景 MCP，独立准备其镜像和配置 |
| hazard-expert-mcp.yaml | 可选隐患专家 MCP，配置见 [专用说明](../../hazard_expert_mcp/README.md) |

当前清单的 Gateway NodePort 是 **30081**。CU 和动态 bucket 使用 **root + privileged**，Web 使用 UID/GID 10001。state 和 OpenCode 程序始终保存在唯一运行节点本地，使用 NFS 也不改变这一点。

一个 CU 读取 `common/frontends.json` 接入多个 frontend；收到请求后创建 bucket，不手工部署固定 runtime。frontend 字段名是 `frontend_id`。各 frontend 的模型、权限和日志配置放在 `common/<frontend_id>/config`，公共资料放在 `app`，用户数据放在 `data/workspaces/<frontend_id>`。完整配置与隔离规则见 [实现说明](../../docs/OPENCODE_BACKEND_IMPLEMENTATION.md)，日志字段与会话历史行为见 [日志与 ID 约定](../../docs/SESSION_LOGGING.md)。
