# 会话日志与事件

每个 frontend 在 `config/session-logging.json` 设置日志，`runtime.logging_file` 指向该文件的容器内路径。字段为 `enabled`、`text`、`reasoning`、`tools`、`tool_arguments`、`tool_output`、`max_chars`；设置只控制 stdout 日志，不改变事件传输。默认全部开启，日志内容上限为 16000 字符。tool_arguments 保留用于兼容既有配置；当前仅记录 part 更新的文本或工具输出，不记录工具参数。

runtime 统一处理原生事件的会话、消息和请求归属，并向 stdout 输出单行 JSON。每条会话日志只有 `user_id`、`frontend_id`、`chat_id`、`content` 四个字段；不附加时间、事件类型、内部 ID 或原始 properties。仅 message.part.updated 输出日志：正文和思考输出该 part 的完整 text，工具输出结果或错误；delta、工具执行前钩子和生命周期事件不输出会话日志。未知身份字段为空，不猜测用户。凭据字段与 Bearer 值脱敏，超过 max_chars 时截断并以省略号结尾。日志设置只影响日志，不裁剪传输的原始事件。

```json
{"user_id":"alice","frontend_id":"web","chat_id":"chat-1","content":"生成的内容"}
```

```bash
kubectl -n opencode-sv logs -f <bucket-pod名称> -c runtime
```

## 唯一事件链

`OpenCode server → GET /global/event (SSE) → BU/runtime → WebSocket → Gateway`。BU 的单个全局订阅覆盖所有用户工作目录；插件不再订阅、构造或转发事件，旧 /plugin/event 接口已删除。被 frontend 过滤规则选中的事件保留原始 payload；未知类型、reasoning 和子会话默认不外发；server.heartbeat 只用于保活，不进入业务队列。工具过程使用原生 message.part.updated 的 tool part，不再额外生成 tool.execute.before 事件。

BU 从全局帧读取 directory 与 payload，沿用原生事件 id 作为 event_id（没有 id 时生成传输 ID）。连接控制事件没有 directory 时使用 global，无法关联的事件不附加用户身份。BU 按 session ID 及已有请求/会话信息附加 frontend_id、user_id、chat_id；精确 request_id 还需要 message 的 parentID 等关联，不能只凭 session ID 猜当前请求。

runtime 将收到的事件与去重标记事务性写入 runtime.sqlite 的 native_pending。独立消费者按接收顺序推导身份和请求归属，按 frontend.event_forwarding 过滤后写入发往 Gateway 的 outbox；路由状态只保存标识和请求执行状态，不额外保存消息历史或拼接 delta，待发送队列仍临时保留原始内容。消费成功后删除待处理项。outbox 满时保留待处理事件；格式或归属错误保存在 native_failed，后续事件继续处理。升级时迁移旧 plugin_pending/plugin_failed 及去重记录，重启后继续消费已接收队列。日志在消费事务成功后输出，stdout 本身不保证跨崩溃恰好一次。

BU 在 SSE 的 server.connected 到达后才就绪，并在创建会话及提交 prompt 前等待连接。断线后撤销 SSE 就绪状态并自动重连，不重复提交 prompt。该接口没有已验证的历史回放保证，因此断线期间的原生事件可能缺失；若缺少完成事件，请求会在原有截止时间失败。已进入 BU 队列的事件仍由 WebSocket ACK/重发保障送达。BU 不用 Last-Event-ID 宣称可恢复缺失事件，也不轮询补查回复。

runtime 收到当前根会话 session.status 的 idle 后结束对应执行，向 CU 只报告执行状态，向 Gateway 发送本轮结果边界及附件信息。Gateway 使用 message/part 快照及选中的 delta 更新渠道消息状态，只有最终结果进入现有渠道投递。对话持久化由 OpenCode 负责；runtime 不回查消息、不轮询回复。

runtime 与 CU 的 WebSocket 传控制状态、ACK，以及不落 outbox 的会话迁移控制（停止会话执行、删除旧会话）。迁移控制用原生 session_id 关联响应，不增加迁移 ID，历史正文不通过 WS 传输。业务事件由 outbox 入队唤醒发送任务；重连重发尚未确认事件，接收端去重。内容走 BU→Gateway 独立 WebSocket。大事件切成 256 KiB 片段，Gateway 持久化并完整重组。Gateway 最终投递和同步 HTTP 等待也使用事件通知。

插件握手版本为 9。CU、runtime 与构建后的插件使用同一版本源码。

## 身份和会话

调用方必须提供 frontend_id、user_id、chat_id。runtime 在用户工作目录创建 OpenCode 会话，同一 frontend/user/chat 始终复用原生 session。不同 chat 使用不同 session，同一用户的文件目录相同。

会话 metadata.bucket_runtime 包含 schema_version、frontend_id、user_id、identity_id、bucket_id、chat_id、input_message_id、requests。runtime 从自己记录的请求、原生会话和消息事件获取身份；无法确定请求的事件不猜测当前请求。子会话身份来自 parentID 关联，工具权限由插件按 session ID 查询 BU，插件检查路径并封装命令；BU 为子会话解析父链。附件工具直接调用发布接口，由 BU 一次完成权限校验及上传。

用户仅在有未完成请求或正在分配时占用 bucket 容量，全部请求结束即释放绑定。下次请求按本 frontend 活跃 bucket 的剩余容量分配；闲置后已停止的 bucket 不因用户曾经使用而重新分配给该用户。用户工作目录保持不变。

CU 在 control.sqlite 的 session 记录中按 frontend/user/chat 保存原生 session_id 和当前 bucket_id。换桶时持有用户锁：先停止旧会话执行，从共享本地 state PVC 将原生会话、子会话、消息、parts 和待办等记录读入 CU 内存，删除旧桶原生会话及本地查询映射，再事务性导入新桶并更新 CU 映射。原生 session_id 不变。旧桶已停止时直接处理其持久化数据库；旧桶在线时由 runtime 调用 OpenCode 的停止和删除接口。历史不拼接进提示词，已有原始审计事件及待送达 outbox 不随会话删除。

迁移不创建持久化中间文件、迁移 ID 或额外的持久化迁移状态。导入遇到数据库错误时持锁重试并保留内存副本；HTTP 断开不取消迁移。接受 CU 在删除源历史到完成目标导入之间崩溃可能导致历史丢失。源/目标 OpenCode 使用相同数据库结构；升级已有安装时按需从旧 runtime.sqlite 的 session 映射发现历史。
