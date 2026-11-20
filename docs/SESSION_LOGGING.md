# 会话日志与事件

每个 frontend 在 `config/session-logging.json` 设置日志，`runtime.logging_file` 指向该文件的容器内路径。字段为 `enabled`、`text`、`reasoning`、`tools`、`tool_arguments`、`tool_output`、`max_chars`；设置只控制 stdout 日志，不改变事件传输。默认全部开启，日志内容上限为 16000 字符。tool_arguments 控制工具调用参数，tool_output 控制工具结果与错误。

runtime 统一处理原生事件的会话、消息和请求归属，并向 stdout 输出单行 JSON。每条会话日志只有 `user_id`、`frontend_id`、`chat_id`、`content` 四个字段；不附加时间、事件类型、内部 ID 或原始事件。原生 session.text.ended 和 session.reasoning.ended 输出完整文本，session.tool.called 输出参数，session.tool.success/failed 输出结果或错误；delta、工具执行前钩子和生命周期事件不输出会话日志。未知身份字段为空，不猜测用户。凭据字段与 Bearer 值脱敏，超过 max_chars 时截断并以省略号结尾。日志设置只影响日志，不裁剪传输的原始事件。

```json
{"user_id":"alice","frontend_id":"web","chat_id":"chat-1","content":"生成的内容"}
```

```bash
kubectl -n opencode-sv logs -f <bucket-pod名称> -c runtime
```

## 唯一事件链

`OpenCode server → GET /api/event (SSE) → BU → WebSocket → Gateway`。单个原生 2.x 全局订阅覆盖所有用户目录；插件不转发内容事件。

BU 读取 2.x 原生 id/type/data/location。目录缺失的生命周期事件通过 sessionID/父会话及当前请求关联，无法归属的事件不外发；原生 id 必须存在，不生成替代 ID。

BU 先把原生事件写入 native_pending，再在同一事务中完成归属、过滤、日志格式化、outbox 入队及终态归约。outbox 满时保留输入；错误事件进入 native_failed。重启后继续消费已接收队列。没有旧插件事件队列的迁移兜底。stdout 不保证跨崩溃恰好一次。

BU 在 SSE 的 server.connected 到达后才就绪，并在创建会话及提交 prompt 前等待连接。断线后撤销 SSE 就绪状态并自动重连，不重复提交 prompt。该接口没有已验证的历史回放保证，因此断线期间的原生事件可能缺失；若缺少完成事件，请求会在原有截止时间失败。已进入 BU 队列的事件仍由 WebSocket ACK/重发保障送达。BU 不用 Last-Event-ID 宣称可恢复缺失事件，也不轮询补查回复。

根会话 execution.succeeded/failed/interrupted 决定执行终态；step 完成与子会话终态不结束请求。插件通过原生 experimental.idle 报告尚未结束的后台工作，成功的前台执行若仍有后台任务则继续等待原生后续轮次。Gateway 从 step 与 text.ended 组装最终正文和用量，工具输出与推理不会被误认为最终答案。

runtime 与 CU 的 WebSocket 传控制状态、ACK，以及不落 outbox 的会话迁移控制（原生历史导出、导入、删除旧会话）。迁移控制用原生 session_id 关联响应，不增加迁移 ID，历史正文不通过 WS 传输。业务事件由 outbox 入队唤醒发送任务；重连重发尚未确认事件，接收端去重。内容走 BU→Gateway 独立 WebSocket。大事件切成 256 KiB 片段，Gateway 持久化并完整重组。Gateway 最终投递和同步 HTTP 等待也使用事件通知。

插件握手版本为 10，基于 @opencode/plugin 2.3.2 的 setup/hooks/tool.transform。CU、runtime 与插件一起更新。

## 身份和会话

调用方必须提供 frontend_id、user_id、chat_id。runtime 在用户工作目录创建 OpenCode 会话，同一 frontend/user/chat 始终复用原生 session。不同 chat 使用不同 session，同一用户的文件目录相同。

会话 metadata.bucket_runtime 包含 schema_version、frontend_id、user_id、identity_id、bucket_id、chat_id、input_message_id、requests。runtime 从自己记录的请求、原生会话和消息事件获取身份；无法确定请求的事件不猜测当前请求。子会话身份来自 parentID 关联，工具权限由插件按 session ID 查询 BU，插件检查路径并封装命令；BU 为子会话解析父链。附件工具调用 BU 发布接口完成权限校验，再由 BU 提交文件快照到 Gateway 统一上传接口；Gateway 按入站请求所属渠道处理并返回附件引用。

用户在有未完成请求、正在分配或迁移尚未完成时占用 bucket 容量；请求和迁移全部结束即释放绑定。下次请求按本 frontend 活跃 bucket 的剩余容量分配；闲置后已停止的 bucket 不因用户曾经使用而重新分配给该用户。用户工作目录保持不变。

CU 在 control.sqlite 按 frontend/user/chat 保存 session_id 和 bucket_id。请求收到原生终态后，还须确认会话树没有活动执行、待处理 inbox 和未消费的后台 jobs，才释放用户绑定。迁移前再次核验；不满足时仍在源桶继续，不强制中断。

迁移持有用户锁，通过 OpenCode 原生 export/import API 转移已完成的对话与工具调用，父会话先于子会话导入，保留 session_id。目标回读确认后删除源会话，清理源 BU 会话索引；审计和待送达 outbox 保留。工作目录继续使用已有共享配置，迁移不读取 OpenCode 内部 SQLite 表。

原生 JSON 暂存于源桶 state 下 session-transfers 目录，CU journal 记录 source/target/step。控制 WS 只携带操作和会话标识，不传历史正文。导入或删除失败时禁止目标续聊；下次提交根据持久化步骤恢复，重复导入须核验归属与完整历史。成功后清理暂存。HTTP 断开仍不取消已接收的迁移；源桶已停止时先在原 state 启动原生服务。
