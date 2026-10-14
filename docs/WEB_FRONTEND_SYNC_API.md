# OpenCode Web 同步请求 API

默认 CU 地址 `http://<节点地址>:30080`，集群内为 `http://container-up.opencode-sv.svc.cluster.local:8000`。

`POST /inbound-sync/{frontend_id}`，示例配置 ID 为 web-yk。请求使用 `Content-Type: application/json`，不需要 API key 或 Authorization 头。

```json
{
  "user_id": "alice",
  "chat_id": "chat-001",
  "request_id": "client-generated-unique-id",
  "content": "分析附件并生成报告",
  "attachments": [{"url": "https://files.example/report.pdf", "filename": "report.pdf"}],
  "metadata": {}
}
```

user_id 必填；chat_id 默认 default；request_id 可选，建议由调用方生成以便去重与超时查询。content、attachments、metadata 可省略。相同用户与 chat_id 继续同一逻辑会话。严格 schema 不接受旧 raw 字段。

响应包含 request_id、status、frontend_id、user_id、chat_id、bucket_id、identity_id、content、attachments、metadata、delivery_state，以及可选 error、partial、usage。HTTP 200 时仍应检查 status：completed、failed、cancelled；不可把传输成功当作执行成功。同步请求不触发渠道回调，因此 delivery_state 可保持 pending，它不是执行状态。

项目统一使用 `user_id` 和 `identity_id`；旧响应字段 `instance_id` 已更名为 `identity_id`，值不变。`chat_id` 是调用方或渠道的聊天标识，在 `frontend_id + user_id` 范围内区分会话。重复的派生字段 `conversation_id` 已删除；`session_id` 是后端调用 OpenCode 使用的原生会话 ID。完整命名和升级说明见 [日志与 ID 约定](SESSION_LOGGING.md)。

默认等待上限由 control.limits.request_seconds 控制（600 秒）。超时返回 504，detail 含 request_id；任务可能仍在执行。查询 `GET /requests/{frontend_id}/{request_id}`，或通过 `POST /requests/{frontend_id}/{request_id}/cancel` 取消。不要换一个 request_id 盲目重试有副作用的请求。相同 request_id 与相同请求内容复用原结果，内容改变返回 409。

异步方式为 `POST /inbound/{frontend_id}`：返回排队状态，最终结果发送到该 frontend 的渠道/回调。同步与异步属于不同请求指纹，不能共用同一个 request_id。

附件只传 URL/路径引用，CU 不替调用方上传文件。最终附件由 publish_attachment 上传 MinIO，结果始终是 URL。详见 [附件协议](ATTACHMENT_UPLOAD_API.md)。

可选 web_server 的 `/inbound` 是异步代理，不要求或转发 Authorization；同步请求直接调用 CU。配置 send_msg_url 为可达的业务回调；使用示例代理接收回调时可设为 `http://web-server:8000/outbound`。
