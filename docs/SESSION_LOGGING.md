# 会话过程日志与 ID 约定

OpenCode 插件将模型实际返回的 reasoning、回复正文、工具调用原始参数、完成结果和错误写入 bucket 容器 stdout，每条记录是一行 JSON。日志不改变渠道消息，也不把思考内容加入最终正文。模型没有返回的内部推理无法记录。

## 配置与查看

每个 frontend 使用自己的 `common/<frontend_id>/config/session-logging.json`，总表该项的 `runtime.logging_file` 指向此文件。示例：

```json
{
  "enabled": true,
  "text": true,
  "reasoning": true,
  "tools": true,
  "tool_arguments": true,
  "tool_output": true,
  "max_chars": 16000
}
```

`enabled` 控制全部插件日志，其余 bool 分别控制回复、思考、工具及工具参数/输出。`max_chars` 限制每条记录的单个内容字段，超出时带 `<字段>_truncated: true`；未超限的文本增量完整输出，结束快照不重复输出已经记录的文本。凭据字段和 Bearer token 会被替换为 `[REDACTED]`，工具的任意文本仍按内容记录。未配置 `logging_file` 时使用上述默认值；显式指定文件缺失或格式错误时启动失败。

修改配置后重启对应 frontend 的桶生效，文件不会挂入用户 Bash 沙箱。查看指定 bucket：

```bash
kubectl -n opencode-sv logs -f <bucket-pod名称> -c runtime
```

安装了 jq 的管理机可以按用户过滤：

```bash
kubectl -n opencode-sv logs -f <bucket-pod名称> -c runtime |
  jq -R 'fromjson? | select(.logger == "session" and .frontend_id == "web-yk" and .user_id == "alice")'
```

主要事件为 `reasoning`、`text`、`tool.start`、`tool.end`、`tool.error`、`session.error`。日志读取失败记录 `logging.error`，不阻断原有工具授权或请求执行。stdout 的留存和轮转遵循集群日志配置，插件不额外写用户工作区文件。

## 全量事件与最终回复链路

插件 event hook 收到的全部事件只经过 `OpenCode 插件 → runtime → CU`。思考、正文增量、工具状态、会话事件、未知事件类型以及结束后的事件均逐条原样推送，不按类型过滤、不节流、不截断。工具执行前的原始参数也会推送。stdout 日志配置仅影响日志输出，关闭日志不会关闭事件推送。

原始事件使用 `opencode.event`，携带稳定 event_id、directory、原始 event 和能够确定的 session_id/message_id/input_message_id/request_id。没有请求归属的启动、会话及子会话事件也转发，不猜测当前请求。runtime 对有关联的事件校验归属并补充可信 frontend_id/user_id/chat_id；去重与 outbox 写入在同一事务中完成。CU 在 SQLite 的 records 表以 kind=opencode_event 保存完整事件，重连重放不会产生重复记录；迟到事件仍保存。

大事件通过同一 WS 链路拆为 `opencode.event.chunk`，每片原始字节最多 256 KiB，再由 CU 完整重组，避免触发 1 MiB 帧限制。分片事务性入队，接收中的分片也持久化，CU 重启后可继续接收；分片本身不触发渠道发送。

最终回复另行从原生事件汇总本轮助手正文和用量，回调携带 request_id、session_id、input_message_id、directory 和递增 revision。只有该结果使请求结束，CU 只将最终正文和附件交给渠道；思考、正文增量、工具及会话状态不触发渠道发送。runtime 不订阅 OpenCode SSE，不轮询、补查最终消息。

插件对失败回调最多尝试三次并复用同一 event_id 或 revision，仍失败则记录错误；最终结果未能送达的请求按截止时间失败，不切换到查询链路。runtime 确认接收后通过持久 outbox 向 CU 重放直至收到 ACK。运行时重启后未收到持久化终态的执行中请求标记中断，不重新执行。插件握手版本为 3，需同步更新 CU、runtime 和构建后的插件。

## metadata 与请求归属

runtime 创建 OpenCode 会话时写入 `metadata.bucket_runtime`，其中包含 `schema_version`、`frontend_id`、`user_id`、`identity_id`、`bucket_id`、`chat_id`、`input_message_id` 和 `requests`。OpenCode 原生 `session_id` 保持由服务生成。

`requests` 保存最近 128 个输入消息 ID 与请求 ID 的对应关系；复用会话时只更新 metadata，不重复 PATCH 权限。已有会话在下一次请求时补齐 metadata。插件从会话创建/更新事件读取身份，缓存未命中时使用 OpenCode 自己的会话和消息接口查询，无需增加 runtime 用户查询 RPC。文本通过 assistant 消息的 parentID 对应原始请求，不把旧事件套用到“当前请求”。

子会话缺少 metadata 时，沿 parentID 读取所属用户。不能确定所属请求的子会话日志保留 `parent_session_id`、`session_id`、`message_id`，省略 `request_id`；不会猜测并关联到父会话后续的新请求。无 managed metadata 且无 managed 父会话的事件不输出业务过程日志。metadata 用于日志归属，授权仍使用 runtime 的请求与会话记录。

## 项目统一 ID 命名

项目内部模型、HTTP/WS 业务字段和日志统一使用 `snake_case`：

| 字段 | 含义 |
| --- | --- |
| `frontend_id` | 注册的业务前端；配置总表也用此名称 |
| `user_id` | frontend 内的用户标识 |
| `identity_id` | frontend_id 与 user_id 的确定性哈希，供分桶、工作区映射使用 |
| `chat_id` | 调用方的聊天标识或渠道群/话题标识，在 frontend_id + user_id 范围内区分聊天；用于回复路由、会话复用与历史恢复 |
| `session_id` | OpenCode 原生会话 ID，桶重建后可能更换 |
| `request_id` | 一次请求的去重与查询标识 |
| `message_id` / `input_message_id` | OpenCode 消息 ID / 本轮输入消息 ID |
| `part_id` / `call_id` | OpenCode 消息片段 / 工具调用标识 |
| `bucket_id` / `profile_id` | 执行桶 / frontend 内模型配置方案 |
| `attachment_id` | 已发布附件，上传与持久化共用此名称 |
| `event_id` / `correlation_id` | WS 可靠事件 / RPC 请求响应关联 |

OpenCode SDK 的 `sessionID/messageID/callID/parentID`、飞书的 `open_id`、QXT 的 `sender_uid` 等外部协议字段在适配边界保留原名；不能修改第三方接口字段。渠道消息 ID 位于渠道 metadata，不能作为 OpenCode message_id 使用。不同用途的标识不拼成同一个字段。

`conversation_id` 已从新请求、响应、metadata 和日志中删除。原来它只是 `frontend_id + user_id + chat_id` 的派生哈希，没有独立业务意义。runtime 仅在数据库查找时计算组合键，沿用原算法和已有记录，不再传递另一套会话 ID。同一 frontend 下，同一用户使用相同 chat_id 继续聊天，换 chat_id 开始独立聊天；不同用户或 frontend 即使 chat_id 相同也不会共用会话。Web 不传 chat_id 时使用 `default`。

本次更名：`usr_id` → `user_id`；`instance_id/user_key` → `identity_id`；附件局部变量 `artifact_id/upload_id` → `attachment_id`；frontend 配置 `id` → `frontend_id`。身份哈希算法、数据库记录主键和用户目录路径保持原值。

旧总表的 `id` 仍作为输入别名接受，序列化输出统一为 `frontend_id`。旧 `/api/message`、`/api/cancel` 的 `usr_id` 仍接受，冲突值拒绝；内部渠道事件和回调 metadata 统一输出 `user_id`。Web 响应中的 `instance_id` 已替换为 `identity_id`，调用方需更新字段名。服务启动时事务性迁移 SQLite 中的旧字段名称及 CU 待发送请求，不删除会话、文件或附件记录。旧桶请求仅存有会话哈希，恢复时兼容读取；在 CU 重放或下一次新请求提供原始 chat_id 前，日志不猜测这个字段。已有 OpenCode metadata 在复用时移除旧 conversation_id。

## 更新已有部署

1. 更新服务器 source，并在 `opencode_plugin` 下运行 `npm ci && npm run build`。
2. 将示例 `session-logging.json` 复制到各自 `common/<frontend_id>/config/`，在对应 frontend 的 `runtime` 中添加 `logging_file`。总表统一使用 `frontend_id`。
3. 更新调用方读取的 `identity_id` 和 `metadata.user_id` 字段；使用 `frontend_id + user_id + chat_id` 关联业务聊天，不再读取 `conversation_id`。
4. 在无执行中请求时重启 CU 和桶，确保新旧字段版本不会混跑。无需重建镜像。

部署全流程见 [Kubernetes 部署说明](../k8s/opencode/README.md)。
