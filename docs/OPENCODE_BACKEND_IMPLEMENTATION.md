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

CU 以 frontend_id + user_id + chat_id 维护会话目录。相同用户不同聊天有独立 session ID 和容量占用，文件目录仍共享该用户的 workspace。不同 frontend 不共享桶。工作目录继续是 `/mnt/env/workspaces/<frontend>/<清理后的 user_id>`，CU 检测清理后路径碰撞并拒绝不同身份共用目录。

路由优先级是已有活动绑定、未完成迁移的目标、历史会话所在原桶。原桶容量未满时继续使用，即使其他桶负载更低；原桶休眠时原地唤醒，回收中则等待停机完成再唤醒。原桶满了或已丢失，才从同 frontend 的可用桶中按运行状态和占用选择；都满了则新建桶。换桶时先迁移父子会话历史并验证，再提交新输入。容量预留在同一个事务中完成，多个会话同时返回不会重复占用最后一个空位。

创建会话调用 `POST /api/session`，传入稳定 session ID、`location.directory` 和可信 `metadata.managed`（frontend_id、user_id、identity_id、chat_id）。CU 在网络调用前保存原生 ID；输入调用 `/prompt`，使用稳定消息 ID、`delivery: queue` 以及消息 `metadata.managed`（request_id、deadline_at）。业务 metadata 不参与身份判断。

原生 inbox 负责执行排队。CU 只重试未确认的 HTTP 入队，同一消息 ID 不会生成第二条输入。会话锁只覆盖分配、创建、迁移和提交，不覆盖执行期。模型/provider/MCP/skills 配置由 OpenCode 原生加载及监听。

## 权限与 shell

插件读取 `MANAGED_BUCKET` 部署配置，从原生会话取身份，从当前原生输入取请求和截止时间；每次使用时读取挂载的 `user-permissions.yaml`。defaults 与匹配 frontend/user 的 overrides 合并，不经 CU/Gateway 查询权限。

子会话沿 parentID 继承根身份。原生 subagent 的 prompt hook 将调用者的 request_id/deadline 写入子输入 metadata；已有子会话的后续工作也获得对应新请求的上下文。

文件工具检查 workspace/public_roots、符号链接、硬链接和私有配置范围。用户不能通过原生管理工具移动会话或操作服务配置。公共资料位于本 frontend 的 app 子树；config 仅供服务加载。模型上下文读取本用户的 AGENTS.md，工具开关与只读策略同时影响工具目录和实际执行检查。

shell 继续由原生工具执行及取消。插件把命令包装为 `timeout → flock → bwrap → shell`，设置私有 PID/挂载空间、清空环境、限定挂载、按策略开放网络；同会话的文件快照与 shell 共用锁。没有 Python shell 执行器。文件工具、shell 和发布工具均使用当前输入的截止时间。

## 回复与附件

Gateway 每桶共用一条原生 `/api/event` SSE，按 CU 保存的 session/input ID 关联业务请求；从 `/api/session/{id}/message` 分页读取持久化消息补齐断线期间的完整文本、工具结果和用量。原生 SSE 不支持本项目可用的历史事件重放，不能承诺恢复丢失的每一段 token delta。

单条请求以原生 idle 及整棵会话树、inbox、后台任务清空判断完成。连续排队输入可能共用一个执行结束标记，此时下一条已消费的原生 user 消息构成上一条回复的边界。Gateway 只用当前请求根会话的最终 assistant 步骤作为正文，子会话不覆盖正文。

Gateway 先持久化最终回复与 SSE 终态，再向 CU 确认 collected。CU 收到确认前保留桶；外部渠道发送失败不会继续占用执行桶。发送中崩溃恢复为 unknown，避免盲目重发。

附件链路见 [附件 API](ATTACHMENT_UPLOAD_API.md)。`publish_attachment` 直接注册在原生工具目录，插件创建文件快照后直接调用 Gateway。模型不能选择渠道、用户或存储凭据。

## 取消、截止时间与迁移

取消尚未消费的输入使用原生 inbox delete。已执行的当前输入通过原生 `interrupt → wait` 清理会话树；下一条已消费输入不被上一条请求的取消误伤。截止时间由 CU 路由同一套取消操作，shell 另有硬超时。

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
