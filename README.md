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

## 配置

- [CU 与 bucket runtime 统一配置](config/opencode/frontend.json)
- [OpenCode server](config/opencode/opencode-server.json)
- [用户权限与目录范围](config/opencode/user-permissions.yaml)

`frontend.json` 保留原有渠道、回调与 MinIO 字段，`control`、`runtime` 统一配置两种服务。初始只启动 CU；请求到达后按用户容量创建/复用 bucket Deployment，runtime 读取同一份配置。`instance_idle_ttl_seconds` 控制整个 bucket 的闲置回收时间，0 表示禁用回收。每个 bucket 只有一个 OpenCode server，不存在每用户进程。

安装 Python 依赖：`python -m pip install -e '.[test]'`。本地开发时修改配置中的路径与服务地址，运行需要 Linux、OpenCode 二进制、编译后的插件和 bwrap。

```bash
python -m agent_backend control --config config/opencode/frontend.json

# Kubernetes 部署下 runtime 由 CU 自动创建，不手动启动。
```

发布镜像、离线资源、权限开关、附件链路、HTTP 接口与部署细节见 [运行说明](docs/OPENCODE_BACKEND_IMPLEMENTATION.md)。

部署文件和完整步骤见 [Kubernetes 部署说明](k8s/opencode/README.md)，包含原服务器目录的 PV/PVC、local/NFS 选择及每请求工作目录重建。

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
