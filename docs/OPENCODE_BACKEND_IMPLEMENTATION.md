# OpenCode 2.x 后端

后端基线为 [OpenCode v2.3.2](https://github.com/anomalyco/opencode/releases/tag/v2.3.2)。构建脚本固定发行包及 SHA-256，启动要求服务报告 2.x；会话迁移按 2.3.2 的 SQLite 表结构实现，升级版本时需同步验证。只有 Gateway 同步 SSE 输出保留 1.x 事件协议。

## 组件职责

```text
渠道 / Web → Gateway → CU → BucketRuntimeAdapter → OpenCodeServer → opencode serve
               ↑                 ↑                    │
               └── 事件 WS ──────┴── /api/event SSE ───┘
                                 ↑
                         原生 2.x 插件 → 私有 RPC
```

- `OpenCodeServer`：子进程、原生 HTTP API、全局 SSE、插件握手与就绪状态。
- `BucketRuntimeAdapter`：请求归属、权限、持久化事件队列、执行终态和附件发布。
- CU：frontend 用户调度、会话位置及跨桶迁移。控制 WS 仅传执行状态和会话控制。
- Gateway：保存 2.x 原生事件、组装渠道最终正文/用量、附件上传与交付。`legacy_events.py` 独立负责同步 SSE 的 2→1 投影。

各服务独立入口：

```bash
python -m channel_gateway --config /mnt/common/frontends.json
python -m container_up --config /mnt/common/frontends.json
python -m bucket_runtime --config /mnt/common/frontends.json --bucket-id "$BUCKET_ID" --frontend-id "$FRONTEND_ID"
```

BU 先启动插件 RPC 和事件消费，再启动 OpenCode；等待其原生数据库迁移完成，健康检查、SSE 和插件均就绪后接单。不同用户有独立 `location.directory`，客户端不修改共享默认目录。原生会话 API 使用全局 session ID。

## 原生 API 与配置热加载

| 操作 | 2.x API |
| --- | --- |
| 健康/版本 | `GET /api/info` |
| 全局事件 | `GET /api/event` |
| 目录上下文 | `GET /api/location?location[directory]=...` |
| 默认模型 | `GET /api/model/default?location[directory]=...` |
| 创建/读取会话 | `POST /api/session` / `GET /api/session/{id}` |
| 元数据和权限更新 | `PATCH /api/session/{id}` |
| 每轮选择当前默认模型 | `POST /api/session/{id}/model` |
| 提交文本 | `POST /api/session/{id}/prompt`，`id/text/delivery` |
| 中断/删除 | `POST /api/session/{id}/interrupt` / `DELETE /api/session/{id}` |
| 等待执行清理完成 | `POST /api/experimental/session/{id}/wait` |

使用 `OPENCODE_PASSWORD` 和 Basic auth。运行时通过 `OPENCODE_CONFIG` 传入真实配置文件；`OPENCODE_CONFIG_CONTENT` 只注入可信插件包目录，不复制原生配置。OpenCode 自己监听配置文件，直接写入或原子替换后按其原生生效边界更新。已有会话每次提交前从有效模型注册表读取默认模型，因此也能使用更新后的模型。

已删除 CU reset HTTP 接口、桶 reset 控制命令、暂停分配状态、进程重建/旧配置回退代码。修改 OpenCode 配置无需重置或重启进程。此机制不包含 `frontends.json`、`user-permissions.yaml`、日志配置和 Python 源码的热加载；这些设置仍按对应服务的启动配置管理。

配置使用原生 `providers/settings/package`、`agents/system`、`permissions`、`plugins`、`skills` 数组、`update`、`snapshots` 和 `mcp.servers`。技能路径填写实际 frontend 的绝对路径，OpenCode 配置正文不做 `{frontend_id}` 模板替换。runtime 自身的配置路径仍支持该占位符。

## 插件与权限

插件使用 `@opencode/plugin@2.3.2` 的默认 `id/setup` 定义，`package.json` 导出 `./server → ./dist/index.mjs`。runtime 的 `opencode.plugin_dir` 默认 `/mnt/source/opencode_plugin`。部署包包含 `package.json` 与 `dist`；运行时不复制或链接 `node_modules`。开发依赖仅用于构建和类型检查。

- `tool.execute.before` 查询 BU `/permissions`，按当前请求检查用户工具开关和路径。原生工具使用 `shell`、`patch`、`read/write/edit.path`。
- shell 命令经逐参数引用的 `timeout → flock → bwrap` 执行。硬截止早于原生 shell 的后台化超时，禁止命令在业务请求结束后继续执行；用户锁与附件发布共用。原生命令管理输出、退出码和取消。
- `session.context` 只在发给模型前还原本插件生成的命令包装。原始事件与 OpenCode 历史保持原样。
- `experimental.idle` 向 BU 报告原生后台工作是否尚未结束。子会话/后台工作的通知由 OpenCode 继续驱动；根执行结束但仍有后台任务时不提前结束业务请求。
- `publish_attachment` 使用原生工具注册接口，并设置 `options.codemode=false`，作为直接调用工具暴露给模型。工具调用 BU `/publish`；BU 校验范围和文件、生成快照，由 Gateway 按渠道上传，按请求、消息、文件清单去重。
- 原生技能目录每次刷新时通过插件筛选，只允许当前 location、本 frontend 的公共 app 和原生虚拟技能。用户目录内的 OpenCode 配置不作为服务配置加载，避免修改用户文件改变可信插件和提供商配置。
- 会话权限使用 `action/resource/effect`，内部路径相对 location，外部公共路径为绝对路径。read 与 edit 范围独立，write/patch 共用原生 edit 权限，插件另查具体工具开关。复用会话时更新当前用户策略并回读校验；写入未生效时拒绝执行。
- 原生 `opencode` 命名空间内的管理工具从直接调用及 Code Mode 清单中移除，执行钩子同时拒绝其有效名称，防止绕开请求归属操作其他会话。插件私有握手版本为 10。
- 保持项目配置发现关闭，防止用户配置加载任意插件。由于 2.x 同时关闭项目规则发现，插件每次生成上下文时单独读取当前 location 的 `AGENTS.md`，拒绝符号链接和硬链接；规则修改随下一次模型请求生效，共享规则继续由原生配置 watcher 管理。

bwrap 仅挂载当前用户工作目录、当前 frontend 公共只读目录和基础运行库，保留 PID namespace、干净环境及 die-with-parent。服务凭据和 config 目录不挂载到 shell 沙箱。OpenCode 主进程是可信服务，内部端口不对用户开放。

## 原生事件与 Gateway 兼容边界

2.x 事件为 `{id,type,created,data,location?,durable?}`。BU 按 sessionID、assistantMessageID、inboxID 和父会话归属关联请求，以原生 id 去重。输入先落 `native_pending`，转发、归约及删除输入在一个事务中提交。outbox 满时保留输入，格式错误记录到 `native_failed`；重连只重发已接收事件。

根会话 `session.execution.succeeded/failed/interrupted` 提供终态候选，原生执行、inbox 和后台 jobs 全部清空后才确认业务终态；step 完成及子会话终态不结束父请求。Gateway 从 `session.step.*` 和 `session.text.ended` 组装最终正文及用量，不能从工具输出或推理片段取最终答案。

显式取消由 BU 调用 `interrupt → wait`，即使 `interrupt` 返回 `interrupted=false` 也等待。原生终态事件可能先于执行清理到达，因此已标记取消的请求由取消流程在等待成功后提交业务终态；清理期间保留用户锁和执行容量。等待失败或超时返回 `CANCEL_FAILED`，桶停止接单。重启恢复也先等待中断清理完成。

Gateway 的 `legacy_events.py` 维护请求/会话隔离的投影状态，将文本、推理、工具、步骤、执行状态等转为 `message.updated`、`message.part.updated/delta`、`session.status/idle/error` 等 1.x 事件。投影与流序号、原生审计、去重在同一事务中提交，断线重放不会重复追加 delta。无 1.x 对应语义的事件仅保留原生审计，不混入旧协议。HTTP 外层 `gateway.start/opencode.event/gateway.result` 和最终响应字段不变。

原生 SSE 是实时订阅，没有自动补齐丢失历史的保证。连接中断时暂停新提交并重连；缺少终态时按请求原截止时间失败，不重复提交或轮询回复。重启前未确认结束的运行请求记为 `EXECUTION_INTERRUPTED`。

## 会话与存储

CU 按 frontend/user/chat 持久化会话位置。业务请求只有在根执行收到终态、会话树无活动执行、inbox 为空且没有运行中或尚未消费结果的后台 jobs 时才结束，随后释放用户的桶绑定。迁移前再次检查这些条件；仍有工作则在源桶继续排队，不通过 interrupt 强制制造迁移条件。

跨桶时持有用户锁，源 BU 调用 `GET /api/experimental/session/{id}/export` 导出已完成的对话和工具调用；子会话逐个导出，父会话先导入。目标 BU 使用 `POST /api/experimental/session/import`，保留 session ID，使用既有用户工作目录和当前权限，清空旧请求路由元数据；回读确认历史一致后，源 BU 才调用原生 `DELETE /api/session/{id}` 并清理本地会话索引。最后 CU 更新位置并提交下一轮请求。工作目录文件由既有共享存储提供。

历史只暂存为共享 state PVC 中的 `runtime/<frontend>/<source-bucket>/session-transfers/<session-key>.json`，不经控制 WS 传输，也不拼进提示词。CU journal 保存 source/target/step；失败保留原会话或已导入副本及暂存文件，下次提交会重试原目标，409 必须核验归属和历史，源删除返回 404 可视为已完成。成功后清理迁移记录和暂存文件。闲置或缺失的源桶在原 state 目录启动原生服务后再导出，不直接读写 OpenCode SQLite 表。inbox、内部事件表、指令运行状态和后台 jobs 不属于迁移内容。

取消流程独立保留 `interrupt → wait`：确认原生清理完成后才结束取消并释放用户锁。详见 [SESSION_LOGGING.md](SESSION_LOGGING.md)。

## 构建与验证

```bash
bash scripts/vendor_opencode_backend.sh
npm --prefix opencode_plugin run typecheck
npm --prefix opencode_plugin run build
npm --prefix opencode_plugin test
python -m pytest tests/backend -q
OPENCODE_TEST_BINARY=/path/to/opencode python -m pytest tests/backend/test_opencode_v2_live.py -q
```

Linux 完整验证使用安装好的 bubblewrap 0.11.0 和 OpenCode 2.3.2：

```bash
OPENCODE_TEST_BINARY=/path/to/opencode python -m pytest tests/backend -q -rs
```

测试模型是本机确定性 HTTP 服务，不调用付费模型。Linux 用例实际执行插件生成的 shell 包装，覆盖文件/网络/PID 隔离、能力与环境变量清理、只读与无访问权限、超时与取消后的子进程清理和锁释放。真实 OpenCode 集成测试同时检查工具清单、越权读写拒绝、附件发布和会话并发；仅通过 TypeScript 类型检查不能替代这条执行链路。

原生导入导出改动前的验证记录（2026-11-20）：临时 QEMU 虚拟机，Debian 13 / Linux 6.12.117、Python 3.13.5、Node 24.22.3、OpenCode 2.3.2，bubblewrap 使用部署固定包 `0.11.0-2+deb13u1`。全量后端测试 **243 passed、0 skipped**；插件测试 **5 passed**，Linux 下类型检查和构建均通过。真实服务验证包含同一进程内配置与用户规则更新、用户本地配置不能加载插件、管理工具不进入 Code Mode、隐藏写工具被直接调用时拒绝执行，以及跨桶迁移后继续原会话。

本次原生导入导出改动在 Windows / Python 3.12 / OpenCode 2.3.2 验证：后端全量 241 passed、19 skipped；随后新增忙目标不阻塞取消的用例，迁移与取消定向回归 50 passed、2 skipped。真实服务用例覆盖已完成对话及工具结果往返、同 session ID 跨桶续聊、源会话删除和原生取消。跳过项需要 Linux bubblewrap/flock 或符号链接权限，本次未重跑 Linux 隔离测试。

vendor 脚本验证 Linux x64 发行包，构建插件和 Python wheels。共享源码需包含插件 `package.json/dist`，不需要部署其开发依赖。构建镜像与安装步骤见 [DEPLOYMENT_MANUAL.md](DEPLOYMENT_MANUAL.md)。Linux 下的 bwrap/文件锁实测需 Linux 主机与对应二进制。
