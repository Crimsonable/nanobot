# opencode_sv


渠道分层：入站 `Gateway → CU → BU`，内容出站 `BU → Gateway`，CU↔BU 保留双向控制 WS。渠道最终外发字段保持不变。部署及旧状态迁移见 [渠道分层说明](docs/CHANNEL_GATEWAY.md)。

以 OpenCode server 为执行后端的多用户渠道服务，每个 bucket 运行一个 OpenCode 实例。

```text
入站：渠道 → channel_gateway → container_up ↔ 控制 WS ↔ bucket_runtime → OpenCode
出站：OpenCode SSE → bucket_runtime → 事件 WS → channel_gateway → 渠道
                                              └─ bwrap / publish_attachment
```

- `channel_gateway`：渠道接入、BU 事件消费、渠道最终回复与投递状态。
- `container_up`：按 frontend 活跃桶容量分配用户、会话位置维护与跨桶历史迁移、控制命令和执行状态。
- `bucket_runtime`：会话归属、事件适配、按 session ID 提供用户路径权限、MinIO 附件发布及 `/cancel`；插件查询 BU 后校验文件操作并封装沙箱命令。
- `agent_backend`：CU 与 runtime 共用的配置、协议和持久化工具库，不包含服务启动器。
- `opencode_plugin`：工具调用 hook、权限查询与命令封装、附件发布工具。原生事件由 BU 订阅 OpenCode server 的 SSE，再附加用户与 chat_id 后经过 frontend 过滤后通过独立 WebSocket 转发 Gateway；会话日志由 BU 输出。
- `config/opencode/<frontend_id>/app/skills`：frontend 的技能与报告脚本。
- `engineering_scene_mcp`：可选的工程场景分析服务。
- `hazard_expert_mcp`：供业务客户端上传附件、调用 OpenCode 隐患分析的入口。

## 配置

- [CU 与 bucket runtime 统一配置](config/opencode/frontends.json)
- [OpenCode server](config/opencode/web-test/config/opencode-server.json)
- [用户权限与目录范围](config/opencode/web-test/config/user-permissions.yaml)
- [会话日志配置](config/opencode/web-test/config/session-logging.json)：按 frontend 控制思考、回复和工具日志，详见 [日志与 ID 约定](docs/SESSION_LOGGING.md)。

`common/frontends.json` 是统一入口总表，Gateway/CU 可同时服务多个 frontend。每个条目的渠道、回调、MinIO、runtime 配置 和闲置 TTL 仅作用于该 frontend；基础设施参数放在总表的 control 中。每个 frontend 的配置统一放在 `common/<frontend_id>/config/`：`opencode-server.json` 配置模型/provider/MCP，`user-permissions.yaml` 配置用户权限。业务资料、skills 和模板分别放在同级 `app/` 下。权限 defaults 只影响该 frontend 的用户。

初始启动一个 Pod，包含 CU（8001）与 Gateway（8000）两个容器，固定单副本；请求到达后创建/复用所属 frontend 的 bucket，不同 frontend 不共享一个 OpenCode 进程。Gateway 入站、查询、取消和 Web 代理不需要 API key；仍校验 frontend 已注册、请求归属和 bucket 身份映射。

安装 Python 依赖：`python -m pip install -e '.[test]'`。本地开发时修改配置中的路径与服务地址，运行需要 Linux、OpenCode 二进制、编译后的插件和 bwrap。

```bash
python -m channel_gateway --config config/opencode/frontends.json
python -m container_up --config config/opencode/frontends.json

# Kubernetes 部署下 runtime 由 CU 自动创建，不手动启动。
```

发布镜像、离线资源、权限开关、附件链路、HTTP 接口与部署细节见 [运行说明](docs/OPENCODE_BACKEND_IMPLEMENTATION.md)。

部署清单统一放在 `k8s/opencode/`，MinIO 清单放在 `k8s/minio/`。可直接执行的部署、镜像导入和接口测试命令见 [部署与测试手册](docs/DEPLOYMENT_MANUAL.md)；清单索引见 [Kubernetes 部署入口](k8s/opencode/README.md)。

全部业务源码与 opencode_plugin 统一放入服务器 source 目录，只读挂载到 /mnt/source。OpenCode 二进制单独放入服务器 opencode/opencode，所有桶只读使用同一份文件。更新 Python 源码、构建后的插件或 OpenCode 二进制后重启服务即可，无需重新构建镜像；Python/系统依赖变化才需要更新基础运行镜像。

## 验证

```bash
python -m pytest tests -q
npm ci --prefix opencode_plugin
npm run build --prefix opencode_plugin
npm test --prefix opencode_plugin
# Linux 上加入真实 OpenCode 集成测试：
OPENCODE_TEST_BINARY=/path/to/opencode python -m pytest tests -q
```

OpenCode 集成测试使用本地模拟模型服务。真实渠道和对象存储需在部署环境验证。

业务公共资料按 `common/<frontend_id>/app/` 分区：OpenCode 用户文件工具、Bash 和 skills 只允许访问当前 frontend 的 app 子树，不能访问 config、frontend 根目录或其他 frontend；用户自己的工作空间继续独立可用。bucket 分别只读挂载本 frontend 的 app/config，config 仅用于服务加载配置，不进入用户 Bash 沙箱。source 只部署服务源码和插件；资料、脚本、skills 和模板统一部署到各 frontend 的 app。

用户工作空间同样按 frontend 分区，路径为 `data/workspaces/<frontend_id>/<user_id>/`；相同 user_id 在不同 frontend 下各有一份文件。bucket 仅挂载本 frontend 的工作区子树，受控文件工具和 Bash 进一步限定为当前用户自己的目录。
