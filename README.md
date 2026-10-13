# opencode_sv

以 OpenCode server 为执行后端的多用户渠道服务，每个 bucket 运行一个 OpenCode 实例。

```text
渠道 / 业务前端 → container_up ↔ WebSocket ↔ bucket_runtime ↔ OpenCode server
                                              └─ bwrap / publish_attachment
```

- `container_up`：渠道接入、用户分桶、附件引用转发和消息投递。
- `bucket_runtime`：会话归属、事件适配、用户权限、工具执行、MinIO 附件发布及 `/cancel`。
- `opencode_plugin`：工具调用 hook 和附件发布工具。
- `public`：公共技能与报告脚本。
- `engineering_scene_mcp`：可选的工程场景分析服务。
- `hazard_expert_mcp`：供业务客户端上传附件、调用 OpenCode 隐患分析的入口。

## 配置

- [CU 与 bucket runtime 统一配置](config/opencode/frontends.json)
- [OpenCode server](config/opencode/web-yk/config/opencode-server.json)
- [用户权限与目录范围](config/opencode/web-yk/config/user-permissions.yaml)

`common/frontends.json` 是统一入口总表，一个 CU 可同时接入多个 frontend。每个条目的渠道、回调、MinIO、runtime profile 和闲置 TTL 仅作用于该 frontend；基础设施参数放在总表的 control 中。每个 frontend 的配置统一放在 `common/<frontend_id>/config/`：`opencode-server.json` 配置模型/provider/MCP，`user-permissions.yaml` 配置用户权限。业务资料、skills 和模板分别放在同级 `app/` 下。权限 defaults 只影响该 frontend 的用户。

初始只启动 CU；请求到达后创建/复用所属 frontend 的 bucket，不同 frontend 不共享一个 OpenCode 进程。CU 入站、查询、取消和 Web 代理不需要 API key；仍校验 frontend 已注册、请求归属和 bucket 身份映射。

安装 Python 依赖：`python -m pip install -e '.[test]'`。本地开发时修改配置中的路径与服务地址，运行需要 Linux、OpenCode 二进制、编译后的插件和 bwrap。

```bash
python -m agent_backend control --config config/opencode/frontends.json

# Kubernetes 部署下 runtime 由 CU 自动创建，不手动启动。
```

发布镜像、离线资源、权限开关、附件链路、HTTP 接口与部署细节见 [运行说明](docs/OPENCODE_BACKEND_IMPLEMENTATION.md)。

部署清单统一放在 `k8s/opencode/`，MinIO 清单放在 `k8s/minio/`。完整步骤见 [Kubernetes 部署说明](k8s/opencode/README.md)，包含 PV/PVC、local/NFS 选择及每请求工作目录重建。

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

业务公共资料按 `common/<frontend_id>/app/` 分区：OpenCode 用户文件工具、Bash 和 skills 只允许访问当前 frontend 的 app 子树，不能访问 config、frontend 根目录或其他 frontend；用户自己的工作空间继续独立可用。bucket 分别只读挂载本 frontend 的 app/config，config 仅用于服务加载配置，不进入用户 Bash 沙箱。`source/public` 仅保存系统通用脚本，不保存 frontend 业务资料。

用户工作空间同样按 frontend 分区，默认路径为 `data/workspaces/<frontend_id>/<user_id>/`；相同 user_id 在不同 frontend 下各有一份文件。哈希布局也保留 frontend 层。bucket 仅挂载本 frontend 的工作区子树，受控文件工具和 Bash 进一步限定为当前用户自己的目录。
