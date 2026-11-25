# opencode_sv


以 OpenCode 2.3.2 server 为执行后端的多用户渠道服务。每个 bucket 容器直接运行一个 `opencode serve`，没有独立的执行适配服务。

```text
入站：渠道 → Gateway → CU → OpenCode 原生 HTTP / inbox
回复：OpenCode 原生 SSE / messages → Gateway → 渠道
附件：OpenCode 插件 → Gateway → 渠道上传接口 / MinIO
权限：OpenCode 插件 → 挂载的用户策略 → 原生文件工具 / bwrap shell
```

- `channel_gateway`：渠道接入、直接读取原生输出、Web SSE 投影、附件上传、回复与投递状态。
- `container_up`：按 frontend 分配桶，维护 user_id + chat_id 与原生 session/input ID 的对应关系，通过原生 API 提交、取消和迁移会话。
- `agent_backend`：共享配置、业务记录和薄原生 HTTP 客户端；没有执行循环、工具执行器或内部 WebSocket 协议。
- `opencode_plugin`：读取用户权限，校验文件范围，给原生 shell 包装 bwrap，直接向 Gateway 发布附件。会话执行、排队、取消、历史存储与模型配置加载由 OpenCode 负责。
- `config/opencode/<frontend_id>/app/skills`：frontend 的技能与报告脚本。
- `engineering_scene_mcp`：可选的工程场景分析服务。
- `hazard_expert_mcp`：供业务客户端上传附件、调用 OpenCode 隐患分析的入口。

## 配置

- [CU 与原生 bucket 配置](config/opencode/frontends.json)
- [OpenCode server](config/opencode/web-test/config/opencode-server.json)
- [用户权限与目录范围](config/opencode/web-test/config/user-permissions.yaml)
- [会话日志配置](config/opencode/web-test/config/session-logging.json)：按 frontend 控制思考、回复和工具日志，详见 [日志与 ID 约定](docs/SESSION_LOGGING.md)。

`common/frontends.json` 是统一入口总表，Gateway/CU 可同时服务多个 frontend。每个条目的渠道、回调、MinIO、bucket 配置 和闲置 TTL 仅作用于该 frontend；基础设施参数放在总表的 control 中。每个 frontend 的配置统一放在 `common/<frontend_id>/config/`：`opencode-server.json` 配置模型/provider/MCP，`user-permissions.yaml` 配置用户权限。业务资料、skills 和模板分别放在同级 `app/` 下。权限 defaults 只影响该 frontend 的用户。

初始启动一个 Pod，包含 CU（8001）与 Gateway（8000）两个容器，固定单副本；请求到达后创建/复用所属 frontend 的 bucket，不同 frontend 不共享一个 OpenCode 进程。Gateway 入站、查询、取消和 Web 代理不需要 API key；仍校验 frontend 已注册、请求归属和 bucket 身份映射。

安装 Python 依赖：`python -m pip install -e '.[test]'`。本地开发时修改配置中的路径与服务地址，运行需要 Linux、OpenCode 二进制、编译后的插件和 bwrap。

```bash
python -m channel_gateway --config config/opencode/frontends.json
python -m container_up --config config/opencode/frontends.json

# Kubernetes 部署下 bucket 由 CU 自动创建，不手动启动。
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
