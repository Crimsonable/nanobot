# 渠道层、CU 与 BU

入站为渠道 → Gateway → CU → BU → OpenCode；内容出站为 OpenCode SSE → BU → Gateway → 渠道。CU 与 BU 继续保持双向可靠 WebSocket，只传执行命令、会话迁移控制和执行状态。

## 职责与接口

Gateway 提供四种接入方式：QXT、飞书、Web 异步 `POST /inbound/{frontend_id}`、Web 同步 `POST /inbound-sync/{frontend_id}`。配置中的 provider 仍为 `qxt/feishu/web`；Web 同步与异步共用一个 provider，由请求接口决定响应方式。隐患专家 MCP 作为普通 Web 同步客户端，继续使用原来的请求和响应协议，不新增渠道或专用适配。

Web 同步接口支持可选 `stream: true`，在同一个 POST 响应中发送请求级原生事件 SSE：`gateway.start` → `opencode.event` → `gateway.result`。Gateway 持久化事件序号、先重放再读取实时事件，支持同一请求携带 `Last-Event-ID` 续传；断开不取消任务。非流式调用保持最终 JSON 响应。具体格式及错误语义见 [Web 同步接口](WEB_FRONTEND_SYNC_API.md#7-流式接收-opencode-原生事件)。

- `channel_gateway`：对外 `/inbound/{frontend_id}`、`/inbound-sync/{frontend_id}`、`/requests/{frontend_id}/{request_id}`、取消与订阅路由。渠道实现及最终 payload 沿用原版本，Web 的 `frontend_id/user_id/chat_id/content/attachments/metadata` 不变，QXT/飞书继续将附件对象 URL 转成文本链接。同步返回字段与原接口一致。
- `container_up`：内部 `/internal/submit/{frontend_id}`、`/internal/requests/{frontend_id}/{request_id}` 及取消、bucket 登记查询；BU 控制连接仍为 `/internal/buckets/ws`。CU 不接收原生内容事件，也不拼装最终回复。
- `bucket_runtime`：原生 SSE 接收、身份关联、过滤、权限与执行。`session.status` 的 busy/retry→idle 驱动当前根会话结束；消息完成快照不结束请求，子会话 idle 不结束父请求。错误和取消分类独立记录。

Gateway 直接接收 BU `/internal/events/ws` 连接，使用 `agent-gateway/1`；CU 通道使用 `agent-bucket/1`。两条连接各自 ACK、重放和去重，使用独立 outbox peer。BU 的原生事件保持原始 payload，增加 frontend/user/chat/request 关联。Gateway 验证 bucket 登记和请求归属；两条内部链路沿用原部署的可信内部网络边界，不增加外部 API key。

CU 终态只接受 `request_id/status/error`，拒绝正文、附件、用量等渠道结果字段。同步等待和投递状态只保存在 Gateway。原 `container_up` 下的渠道、附件、回复拼装兼容模块已删除，调用方应直接导入 `channel_gateway`；公共入站模型在 `agent_backend.ingress`。

## 配置

Gateway 的监听地址、端口、状态目录及内部通信地址使用代码默认值，不属于 frontend 配置。Gateway 默认监听 `0.0.0.0:8000`，状态目录为 `/data/gateway`，通过 `http://127.0.0.1:8001` 调用同 Pod 的 CU；CU 默认监听 8001，BU 默认连接 `ws://channel-gateway:8000/internal/events/ws`。`frontends.json` 不需要 `gateway` 配置块或 CU 端口配置。

各 frontend 的 `event_forwarding` 配置 `include_types`、`include_part_types`、`include_child_sessions`。默认传消息/part 更新、delta、删除、会话状态及错误；part 默认 text/file/tool，不外发 reasoning 和子会话。未知事件默认不外发。按 part 类型过滤 delta 依赖 BU 持久化的 part 类型映射，不能只按事件名放行。每个请求在接受时保存过滤配置快照。

流式 Web 同步请求例外：`stream` 随提交经 CU 传到 BU，BU 绕过上述内容过滤，转发可归属本请求的原生事件，包括 reasoning、未知类型和子会话；身份关联及工作目录校验保持不变，全局心跳和无法关联的事件不外发。Gateway 保留原生 JSON，不转换 AI SDK 协议；有序终态后的迟到事件不进入已结束的 SSE 日志。

现有渠道最终回复依赖 text、message/part 快照、status 和 error，因此配置校验禁止删掉这些依赖。过滤仅作用于对外事件，不影响控制状态。日志配置不决定外发过滤。附件仍由 BU 上传 MinIO，Gateway 只接收对象引用。

## 可靠性与回收

Gateway 持久化入站请求后提交 CU；提交结果不确定时使用同一 request_id 恢复，确定拒绝不自动重试。BU 的状态与 outbox 在同一事务写入。Gateway 持久化事件后 ACK，外部渠道发送独立记账；发送中崩溃恢复为 unknown，不盲目重发。

执行结束释放执行名额。CU 回收前通过控制 WS 请求 drain：BU 无活动请求、原生待处理队列清空、Gateway outbox 获确认后，关闭新事件接收并答复；随后 CU 才 scale 到 0。不等待外部渠道发送成功。Gateway 积压时 BU 报告不可调度，控制连接仍可接收取消等命令。CU 重启后必须重新完成 drain 握手。

OpenCode SSE 仍没有本项目已验证的历史回放保证；这次拆层不补齐未收到的原生事件。Gateway 收到结束标记而缺少完整回复时记录 `NATIVE_REPLY_INCOMPLETE`，不阻塞后续事件，也不虚构正文。

## 部署及旧版本升级

运行入口为 `python -m channel_gateway --config /mnt/common/frontends.json`。Gateway 使用现有 CU 依赖镜像并覆盖启动参数，也提供 `Dockerfile.channel_gateway`。三个组件同步更新源码。

`k8s/opencode/backend.yaml` 只部署一个 `container-up` Deployment，固定一个副本、使用 Recreate 更新策略。同一个 Pod 内运行 `control` 与 `gateway` 两个容器，共同启停和更新，不配置自动扩容。CU 监听 8001，Gateway 监听 8000，并通过 `http://127.0.0.1:8001` 调用 CU。两者共享 state PVC，但分别写 `/data/control/control.sqlite` 与 `/data/gateway/gateway.sqlite`，各自单写；临时目录独立。Kubernetes ServiceAccount token 仅挂到 CU 容器。

保留两个 Service：`container-up:8000` 转到 CU 的 8001，`channel-gateway:8000` 转到 Gateway 的 8000。BU 的双向控制 WS 和独立事件 WS 地址均不变；BU 仍由 CU 动态创建和回收。外部 NodePort 30081 指向 Gateway，Web 代理调用 `http://channel-gateway:8000`。其他业务调用端和渠道回调也应指向 Gateway；保留同一个外部 NodePort 时原地址可继续使用。合并部署不改变渠道最终字段或事件过滤逻辑。

若已经运行过两个独立 Deployment，暂停入站并排空请求与可靠队列后，先将 `container-up` 和 `channel-gateway` 都 scale 到 0，等待旧 Pod 退出。若实际 `/mnt/common/frontends.json` 包含旧的 `gateway` 配置块或显式 `control.port`，将其移除以使用内部默认值，然后删除旧的 **Deployment** `channel-gateway`（保留同名 Service 和 PVC），再应用新 `backend.yaml`。仅 apply 不会删除旧 Deployment，旧 Gateway 若仍运行会争用同一个数据库。这种合并使用原有两个数据库，不需要再迁移记录。

运维入口统一使用 `deployment/container-up`：`kubectl logs ... -c control` 查看 CU，`-c gateway` 查看 Gateway；exec 同样指定容器。首次部署直接使用内置端口默认值。

Web 代理使用 `GATEWAY_BASE_URL` 指向 Gateway。隐患专家 MCP 的调用代码及 `CONTAINER_UP_*` 配置名保持原样，其 `CONTAINER_UP_BASE_URL` 指向提供 Web 同步接口的 Gateway 即可；若外部入口地址不变，客户端无需修改。仓库内 Kubernetes/Compose 示例仅调整目标地址。BU 的 `container-up` 控制 Service 地址仍保留。

旧版本升级必须先暂停入站，等待旧请求结束或取消，并等待 CU/BU 可靠队列清空，然后停止旧 CU 与 bucket。不能混跑新旧 BU 协议。备份数据库后，在旧 CU 和新 Gateway 均停止时运行：

```bash
python -m channel_gateway.migrate \
  --source /data/control/control.sqlite \
  --destination /data/gateway/gateway.sqlite
```

迁移只读旧数据库，保留 request_id、正文、附件、metadata 和发送状态；sending 转为 unknown，sent/skipped 不再发送。命令可重复运行，不覆盖已导入请求；未完成请求或 CU 未确认命令会阻止迁移。迁移后部署新 CU、Gateway 和更新后的 BU，再恢复入站。CU 的 bucket 和 session 记录继续使用原数据库。
