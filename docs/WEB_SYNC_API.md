# Web 单接口回复 API

新增 `POST /inbound-sync/{frontend_id}`。请求字段与现有 `POST /inbound/{frontend_id}` 相同；调用方在同一个 HTTP 响应中取得 AI 的最终回复，无需提供回复回调接口。

```bash
curl -X POST 'http://<container-up-host>:8080/inbound-sync/web-wd' \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"web-demo-1","chat_id":"web-chat-1","content":"你好","attachments":[],"metadata":{},"raw":{}}'
```

成功时返回 HTTP 200：

```json
{
  "status": "completed",
  "frontend_id": "web-wd",
  "user_id": "web-demo-1",
  "chat_id": "web-chat-1",
  "bucket_id": "bucket-0001",
  "instance_id": "web-demo-1",
  "content": "你好！",
  "attachments": [],
  "metadata": {}
}
```

附件走现有出站附件处理，若前端配置了 MinIO，`attachments` 中返回可访问的对象 URL。新接口会忽略进度和流式片段，只返回最终回复。原有 `/inbound/{frontend_id}`、`/outbound` 和回调流程继续可用。

投递失败或附件处理失败返回 HTTP 503；等待超过 `SYNC_REPLY_TIMEOUT_SECONDS` 返回 HTTP 504。默认等待时间为 600 秒，调用方及反向代理的超时应大于此值。当前部署的 `container-up` 为单实例；同步等待使用实例内存配对请求与回复，若将它扩为多个实例，需要改用共享的回复存储。
