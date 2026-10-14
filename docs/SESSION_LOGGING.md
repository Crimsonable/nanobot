# 会话日志与事件

每个 frontend 在 `config/session-logging.json` 设置日志，`runtime.logging_file` 指向该文件的容器内路径。字段为 `enabled`、`text`、`reasoning`、`tools`、`tool_arguments`、`tool_output`、`max_chars`；设置只控制 stdout 日志，不改变事件传输。默认全部开启，日志内容上限为 16000 字符。

插件统一处理原生事件的会话、消息和请求归属。日志消费同一份事件，输出 JSON，包含 timestamp、logger、event、event_id、session_id、message_id、request_id，以及已知的 frontend_id、user_id、chat_id、bucket_id。原生事件 properties 位于 data，超过上限时 truncated=true。凭据字段与 Bearer 值脱敏。插件不查询 OpenCode 的会话或消息接口来补日志。

```bash
kubectl -n opencode-sv logs -f <bucket-pod名称> -c runtime
```

## 唯一事件链

`OpenCode 插件 → runtime → CU`。插件不生成第二套进度或结果协议。思考、正文、工具、会话、未知类型、终态后的事件均原样转发；工具执行前发送原始参数。

插件在 bucket 的持久 state 目录保存待发送事件，写入完成后才返回。runtime 确认接收后删除队列文件。连接失败时保留事件并使用相同 event_id 重发，进程重新启动后继续发送。日志与用户工作区不承担队列存储。

runtime 验证有关联事件的 session、input message 和 directory，附加可信身份，将原始事件和去重标记事务性保存。runtime 独自根据原生消息及 parts 判定最终回复和用量；CU 保存全部原始事件，仅将最终正文、附件交给渠道。没有 SSE、消息回查或最终回复轮询。

runtime 与 CU 的 WebSocket 传可靠业务事件、ACK，以及不落 outbox 的会话迁移控制（停止会话执行、删除旧会话）。迁移控制用原生 session_id 关联响应，不增加迁移 ID，历史正文不通过 WS 传输。业务事件由 outbox 入队唤醒发送任务；重连重发尚未确认事件，接收端去重。大事件切成 256 KiB 片段，CU 持久化并完整重组。CU 最终投递和同步 HTTP 等待也使用事件通知。

插件握手版本为 4。CU、runtime 与构建后的插件使用同一版本源码。

## 身份和会话

调用方必须提供 frontend_id、user_id、chat_id。runtime 在用户工作目录创建 OpenCode 会话，同一 frontend/user/chat 始终复用原生 session。不同 chat 使用不同 session，同一用户的文件目录相同。

会话 metadata.bucket_runtime 包含 schema_version、frontend_id、user_id、identity_id、bucket_id、chat_id、input_message_id、requests。插件从原生会话和消息事件获取身份；无法确定请求的事件保留其原始 ID，不猜测当前请求。子会话身份来自已收到的 parentID 关联，工具授权仍由 runtime 检查。

用户仅在有未完成请求或正在分配时占用 bucket 容量，全部请求结束即释放绑定。下次请求按本 frontend 活跃 bucket 的剩余容量分配；闲置后已停止的 bucket 不因用户曾经使用而重新分配给该用户。用户工作目录保持不变。

CU 在 control.sqlite 的 session 记录中按 frontend/user/chat 保存原生 session_id 和当前 bucket_id。换桶时持有用户锁：先停止旧会话执行，从共享本地 state PVC 将原生会话、子会话、消息、parts 和待办等记录读入 CU 内存，删除旧桶原生会话及本地查询映射，再事务性导入新桶并更新 CU 映射。原生 session_id 不变。旧桶已停止时直接处理其持久化数据库；旧桶在线时由 runtime 调用 OpenCode 的停止和删除接口。历史不拼接进提示词，已有原始审计事件及待送达 outbox 不随会话删除。

迁移不创建持久化中间文件、迁移 ID 或额外的持久化迁移状态。导入遇到数据库错误时持锁重试并保留内存副本；HTTP 断开不取消迁移。接受 CU 在删除源历史到完成目标导入之间崩溃可能导致历史丢失。源/目标 OpenCode 使用相同数据库结构；升级已有安装时按需从旧 runtime.sqlite 的 session 映射发现历史。
