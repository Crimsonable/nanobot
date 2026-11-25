# OpenCode 原生执行桶

固定使用 [OpenCode 2.3.2](https://github.com/anomalyco/opencode/releases/tag/v2.3.2)。构建脚本校验发行包 SHA-256，CU 校验原生版本及数据库升级状态。升级 OpenCode 时同时验证原生协议和插件类型。

## 进程与职责

桶容器直接执行：

```bash
/opt/opencode/opencode serve --hostname 0.0.0.0 --port 4096
```

CU 和 Gateway 继续使用各自 Python 入口。没有桶内 Python HTTP 服务、插件 RPC、控制/内容 WebSocket、执行队列和 outbox。Python 仅作为镜像中的工具链，以及插件按需启动的本地文件快照助手使用。

CU 根据 Kubernetes Pod IP 找到原生端点，保存每桶密码，使用原生 Basic auth。桶重建时重新发现端点。探针访问原生 `/api/info`；CU 接单前还等待原生 `/api/experimental/migration/v1` 完成。Kubernetes 管进程重启，CU 不拉起 OpenCode 子进程。

## 请求、会话和排队

CU 以 frontend_id + user_id + chat_id 维护会话目录，保存原生 session ID 与当前 bucket ID。相同用户不同聊天有独立 session ID；pool 下独立占用容量，individual 下共用固定用户桶且不计算容量。文件目录仍共享该用户的 workspace。不同 frontend 不共享桶。工作目录继续是 `/mnt/env/workspaces/<frontend>/<清理后的 user_id>`，CU 检测清理后路径碰撞并拒绝不同身份共用目录。

pool 路由优先使用符合当前模式的活动绑定、未完成迁移的目标、历史会话所在共享桶。原桶容量未满时继续使用，即使其他桶负载更低；原桶休眠时原地唤醒，回收中则等待停机完成再唤醒。原桶满了或已丢失，才从同 frontend 的共享桶中按运行状态和占用选择；都满了则新建桶。individual 固定路由到用户桶，不参考剩余容量。两种模式都在源桶与目标桶不同时先迁移父子会话历史并验证，再提交新输入。

创建会话调用 `POST /api/session`，传入稳定 session ID、`location.directory` 和可信 `metadata.managed`（frontend_id、user_id、identity_id、chat_id）。CU 在网络调用前保存原生 ID；输入调用 `/prompt`，使用稳定消息 ID、`delivery: queue` 以及消息 `metadata.managed`（request_id、deadline_at）。业务 metadata 不参与身份判断。

原生 inbox 负责执行排队。CU 只重试未确认的 HTTP 入队，同一消息 ID 不会生成第二条输入。会话锁只覆盖分配、创建、迁移和提交，不覆盖执行期。模型/provider/MCP/skills 配置由 OpenCode 原生加载及监听。

## 权限与 shell

插件读取 `MANAGED_BUCKET` 中的启动参数，从原生会话取身份，从当前原生输入取请求和截止时间。两种模式都在使用权限时直接读取挂载的权限文件，支持 JSON 和 YAML，defaults 与匹配 frontend/user 的 overrides 合并。individual 的 CU 也在每次生成 Deployment 时读取该文件并生成挂载；权限内容不写入桶记录或环境变量，不保留 `individual_policy` 快照。

子会话沿 parentID 继承根身份。原生 subagent 的 prompt hook 将调用者的 request_id/deadline 写入子输入 metadata；已有子会话的后续工作也获得对应新请求的上下文。

pool 文件工具检查 workspace/public_roots、符号链接、硬链接和私有配置范围；config 仅供服务加载。individual 不进行逐工具文件范围检查，config 可读且以只读挂载防止修改。两种模式仍隐藏原生会话管理工具，读取本用户的 AGENTS.md，并保留工具开关与业务上下文校验。

shell 继续由原生工具执行及取消。pool 插件把命令包装为 `timeout → flock → bwrap → shell`，设置私有 PID/挂载空间、清空环境、限定挂载、按策略开放网络；同会话的文件快照与 shell 共用锁。individual 不包装命令，原样保留原生 shell 的 command、workdir、timeout 和 background 参数；CU 负责请求截止取消。individual 附件快照不依赖 shell 锁，复制时校验文件元数据，发现并发修改即拒绝该次发布。没有 Python shell 执行器。

## 桶模式与按需切换

在 `frontends.json` 对应条目中配置：

```json
"bucket": {
  "behavior": "individual",
  "permissions_file": "/mnt/common/{frontend_id}/config/user-permissions.yaml"
}
```

`behavior` 只接受 `pool` / `individual`，省略时为 `pool`。individual ID 为 `opencode-user-` 加现有 `Identity.identity_id` 的前 32 个十六进制字符；摘要使用规范 JSON 编码的 frontend_id + user_id，不包含 chat_id，不依赖配置的共享池名称前缀。桶记录保存完整 owner 并校验，不用额外的用户到随机桶映射表。

修改总表后重启 CU 加载配置。切换只改变后续新请求的目标选择，已提交请求仍在原桶完成。源会话有活动工作或未采集输出时，CU 返回可重试的 503，Gateway 保留请求并重试，不回退到旧模式执行。只等待该会话，不排空整个 frontend。未访问的会话保留原桶记录和历史；不能因为 frontend 改了模式就删除旧桶数据。

源桶等于目标桶则恢复原 session；不等则共用 `SessionManager.prepare/transfer` 导出、导入和验证。原生 session ID、子会话、消息及用户工作区路径保持不变。迁移中断时先恢复已经记录的目标，再按当前模式判断是否还需迁移；新输入最终只提交给当前模式的目标。

每桶只保存实际 `behavior`、owner、身份和生命周期状态，不保存 `settings` / `runtime` 配置副本。创建或恢复桶时，用当前已加载的 frontend 配置、桶自身模式和 owner 生成启动参数，并现读权限文件。frontend 总表变更仍需重启 CU 加载；已创建的桶保留自身模式。桶记录、Deployment 和插件统一使用当前结构，不补旧字段或转换旧元数据。仅切换模式时保持 workspace/state 路径不变，已有数据路径的调整仍需单独迁移。运行中端点端口直接取自实际 Pod。`render_bucket_updates` 从已有 Deployment 提取桶身份、实际模式和 owner，其他配置来自当前 frontend，挂载权限现读文件，因此运行该工具的环境必须能访问对应权限文件。

individual 不使用 `max_sessions_per_bucket`；同用户不同聊天仍可以并发。空闲 TTL 与安全停机条件共用，`instance_idle_ttl_seconds: 0` 禁止自动缩容。休眠和 Deployment 丢失后恢复同一个固定 ID 与状态目录，先确认旧实例退出。状态卷丢失导致历史找不到时显式报错，不静默创建空会话。

individual 挂载仅包括用户工作区、当前 frontend 的 app/config、当前桶状态、插件/程序以及显式公共目录。config 固定只读，不挂全局 frontends.json，不挂整个 frontend 工作区或状态卷。执行 UID/GID 为 0，取消 privileged、禁止提权、移除全部 capabilities、使用只读根文件系统；写入限定在配置为可写的卷中。

用户权限默认在 `user-permissions.yaml`，也可将 permissions_file 指向 config 目录中的 JSON 文件：`filesystem.workspace` 支持 read_write/read_only/none。none 不挂真实用户数据，仅提供只读空工作目录。permissions_file 可设 null，individual 使用默认读写个人目录。插件的工具开关每次读取文件，对后续调用生效；文件读写仍由挂载执行，更新权限文件不会自动改变运行中 Pod 的挂载。桶下次由 CU 创建/从休眠唤醒时重新生成挂载；若需立即应用挂载变更，先排空该桶再由 CU 重建 Pod。

额外公共目录可以声明为：

```json
"mounts": [
  {
    "name": "shared-docs",
    "volume": "common",
    "sub_path": "shared/docs",
    "mount_path": "/mnt/shared/docs",
    "access": "read_only"
  }
]
```

额外挂载用于 individual，来源限制在 common PVC 的 shared/ 或当前 frontend 的 app/ 子树，挂载位置限制在 /mnt/shared/ 下，不能覆盖 config、状态或其他用户目录。管理员预先准备公共目录。权限文件可用 `filesystem.mounts: {shared-docs: read_write}` 或 none 覆盖指定挂载。pool 继续使用原有 public_roots 策略；individual 不接受旧 filesystem.public_roots，须改为显式 mounts。

individual 保留 session.tools 开关，但不使用 sandbox.enabled 隔离。旧 sandbox.network=false 的 shell 级断网语义无法由原生 shell 实现，individual 拒绝该配置，不会静默忽略。请求取消、子会话上下文、附件发布和 Gateway SSE 在两种模式下共用。

## 回复与附件

Gateway 每桶共用一条原生 `/api/event` SSE，按 CU 保存的 session/input ID 关联业务请求；从 `/api/session/{id}/message` 分页读取持久化消息补齐断线期间的完整文本、工具结果和用量。原生 SSE 不支持本项目可用的历史事件重放，不能承诺恢复丢失的每一段 token delta。

单条请求以原生 idle 及整棵会话树、inbox、后台任务清空判断完成。连续排队输入可能共用一个执行结束标记，此时下一条已消费的原生 user 消息构成上一条回复的边界。Gateway 只用当前请求根会话的最终 assistant 步骤作为正文，子会话不覆盖正文。

Gateway 先持久化最终回复与 SSE 终态，再向 CU 确认 collected。CU 收到确认前保留桶；外部渠道发送失败不会继续占用执行桶。发送中崩溃恢复为 unknown，避免盲目重发。

附件链路见 [附件 API](ATTACHMENT_UPLOAD_API.md)。`publish_attachment` 直接注册在原生工具目录，插件创建文件快照后直接调用 Gateway。模型不能选择渠道、用户或存储凭据。

## 取消、截止时间与迁移

取消尚未消费的输入使用原生 inbox delete。已执行的当前输入通过原生 `interrupt → wait` 清理会话树；下一条已消费输入不被上一条请求的取消误伤。截止时间由 CU 路由同一套取消操作，pool shell 另有包装器硬超时，individual 使用原生 shell 超时语义。

会话迁移由 CU 调用原生 export/import API：确认源无活动/待消费工作，持久化导出，父会话先导入，逐个回读验证 ID、身份、目录和完整消息；再次确认源未变化后才删除源会话。HTTP 回复丢失可以重试，迁移步骤持久化在 CU。工作区保留在共享卷，不拷贝；不读写原生内部数据库表。

## 配置与验证

`frontends.json` 的条目使用 `bucket` 配置，包含原生二进制/端口/配置路径、workspace/common/state 路径、权限文件、bwrap 参数和附件限制。`gateway_url` 是 HTTP 地址。旧 runtime 配置块、RPC/health 端口、控制 WS 和 event_forwarding 配置已经移除。

```bash
python -m container_up --check-config --config config/opencode/frontends.json
npm ci --prefix opencode_plugin
npm run build --prefix opencode_plugin
npm run typecheck --prefix opencode_plugin
npm test --prefix opencode_plugin
python -m pytest tests -q
OPENCODE_TEST_BINARY=/opt/opencode/opencode python -m pytest tests/backend -q
```

真实执行测试使用本地确定性模型服务，不调用付费模型。Kubernetes 集群、真实渠道与对象存储仍需部署环境验收。部署与升级步骤见 [部署手册](DEPLOYMENT_MANUAL.md)。
