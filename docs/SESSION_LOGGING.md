# 日志、身份与原生会话

会话输出日志由 Gateway 打印，链路为 `OpenCode /api/event + /api/session/{id}/message → Gateway`。普通请求和流式请求共用原生输出读取逻辑；没有内容转发服务和事件过滤队列。

每行结构为：

```json
{"user_id":"alice","frontend_id":"web-test","chat_id":"chat","content":"回复或工具内容"}
```

frontend 的 `bucket.logging_file` 指向 session-logging.json，可配置 enabled、text、reasoning、tools、tool_arguments、tool_output、max_chars。配置在该 frontend 的 Gateway 日志器首次使用时加载，修改后重启 Gateway。字段中的 token/password/authorization 等做脱敏，不打印整个原生 metadata。stdout 不能承诺跨崩溃恰好一次。

## ID 与存储

业务请求使用 request_id；CU 持久化其原生 session_id、input message_id、directory 和 bucket_id。聊天归属键是 frontend_id + user_id + chat_id；工作区键是 frontend_id + user_id。

根会话 `metadata.managed` 保存可信 frontend_id、user_id、identity_id、chat_id。每条原生 user 输入 `metadata.managed` 保存 request_id 与 deadline_at，不把业务身份混入模型提示。子会话沿 parentID 取根身份，其 prompt hook 继承当前调用输入的请求上下文。

原生全局 SSE 提供实时事件，不提供可用的历史事件重放。Gateway 断线后重新读持久化消息分页，恢复完整文本、工具结果、结束标记与用量；丢失的逐 token delta 不会被伪造为原始事件。Web SSE 投影和最终结果保存在 Gateway，可以按 Last-Event-ID 重放。

回复以当前输入到下一条已消费原生 user 输入之间的消息为范围。无后续输入时，还需原生 idle、整棵会话树和后台任务清空。子会话输出参与流与日志，但不作为根会话最终正文。工具完成不代表请求完成。

## 迁移

CU 持久化迁移步骤，通过原生 export/import API 迁移根与子会话，保留原生 ID、完整消息及工具调用。目标逐个回读验证成功、源历史未变后，才按子到父顺序删除源。迁移暂存放在 CU 的 session-transfers 下，完成后清理。代码不访问原生 SQLite 表。

空闲回收还等待 Gateway 的 collected 确认，避免输出持久化前关闭桶；不等待渠道投递成功。重启恢复依靠业务记录与原生存储，不重建执行队列。
