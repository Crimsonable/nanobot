# Bridge Service

这是 `nanobot/channels/bridge.py` 对应的独立服务端实现，放在 `nanobot/` 包外。

## 目录

- `server.py`
  独立 bridge 服务。接收用户侧 client 消息，转发给 nanobot 的 `bridge` channel。
- `client_demo.py`
  最小客户端示例。
- `protocol.py`
  协议辅助函数。

## 协议

client -> bridge:

```json
{
  "type": "message",
  "request_id": "req_xxx",
  "conversation_id": "conv_1",
  "user_id": "u_1",
  "tenant_id": "default",
  "content": "hello",
  "attachments": [],
  "metadata": {}
}
```

bridge -> nanobot:

```json
{
  "type": "inbound_message",
  "version": 1,
  "request_id": "req_xxx",
  "tenant_id": "default",
  "conversation_id": "conv_1",
  "session_key": "remote:default:conv_1",
  "channel": "bridge",
  "sender_id": "u_1",
  "chat_id": "conv_1",
  "content": "hello",
  "attachments": [],
  "metadata": {}
}
```

nanobot -> bridge -> client:

- `progress`
- `final`
- `error`
- `cancelled`

## 运行

1. 启动 bridge 服务：

```bash
python -m bridge_service.server
```

2. 启动 nanobot，并启用 `bridge` channel：

```bash
nanobot gateway --config ~/.nanobot/config.json
```

配置示例：

```json
{
  "channels": {
    "bridge": {
      "enabled": true,
      "bridgeUrl": "ws://127.0.0.1:8766",
      "bridgeToken": "",
      "allowFrom": ["*"]
    }
  }
}
```

3. 发送测试消息：

```bash
python -m bridge_service.client_demo --content "帮我分析一下这个异常"
```

## 说明

- `client-port` 默认 `8765`，面向业务侧 client。
- `bot-port` 默认 `8766`，面向 nanobot 的 `bridge` channel。
- 如果设置了 `--token`，client 和 nanobot 两端都要先发 `{"type":"auth","token":"..."}`。
- `cancel` 请求建议带上原始 `user_id`，这样 nanobot 侧 `/stop` 也能通过 `allowFrom`。
