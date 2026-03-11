# Bridge Service

`bridge_service/` 现在只保留最核心的桥接能力:

- 用户侧 HTTP:
  - `POST /api/messages`
  - `POST /api/cancel`
- `nanobot` 侧 WebSocket:
  - `WS /`
- 健康检查:
  - `GET /healthz`

## 运行

```bash
cd bridge_service
uv sync
uv run nanobot-bridge-service --host 127.0.0.1 --port 8766 --token my-shared-token
```

## nanobot 配置

```json
{
  "channels": {
    "bridge": {
      "enabled": true,
      "bridgeUrl": "ws://127.0.0.1:8766",
      "bridgeToken": "my-shared-token",
      "allowFrom": ["*"]
    }
  }
}
```

## HTTP 示例

```bash
curl -X POST http://127.0.0.1:8766/api/messages \
  -H 'Content-Type: application/json' \
  -H 'X-Bridge-Token: my-shared-token' \
  -d '{
    "conversation_id": "conv-1",
    "user_id": "user-1",
    "tenant_id": "default",
    "content": "hello"
  }'
```

```bash
curl -X POST http://127.0.0.1:8766/api/cancel \
  -H 'Content-Type: application/json' \
  -H 'X-Bridge-Token: my-shared-token' \
  -d '{
    "conversation_id": "conv-1",
    "user_id": "user-1",
    "tenant_id": "default",
    "request_id": "req_xxx"
  }'
```

## Demo

```bash
uv run nanobot-bridge-demo --content "hello"
```
