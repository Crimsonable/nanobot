# Gateway 与原生 OpenCode

入站：渠道 → Gateway → CU → OpenCode 原生 HTTP。出站：OpenCode SSE/消息记录 → Gateway → 渠道。附件：原生插件 → Gateway → 渠道媒体服务或 MinIO。

Gateway 提供 QXT、飞书、Web 异步 `/inbound/{frontend_id}` 和 Web 同步 `/inbound-sync/{frontend_id}`；查询与取消使用 `/requests/{frontend_id}/{request_id}`。同步流格式见 [Web API](WEB_FRONTEND_SYNC_API.md)。渠道回复字段保持不变。

CU 仅保存业务身份、原生 ID、桶位置、请求状态和迁移步骤。Gateway 直接订阅原生 SSE，并通过持久化消息记录恢复完整回复；CU 不接收文本、推理或工具输出。权限和文件快照都位于原生插件。

## 可靠性

Gateway 入站先落库，CU 使用稳定原生输入 ID 入队，网络超时不换 ID。原生 inbox 负责排队执行。Gateway 对收到的原生事件去重，并把消息快照投影到已有 Web SSE 格式，正文和用量按请求边界重建。全局 SSE 是实时流，断线时无法恢复每一个增量事件，但完整消息可以恢复。

最终回复先写 Gateway，再向 CU 确认 collected；未确认前桶不回收。渠道投递独立记账，sending 在重启后变为 unknown，不能据此盲目重发。原生 idle、连续排队输入边界、子会话和后台任务的处理见 [实现说明](OPENCODE_BACKEND_IMPLEMENTATION.md)。

## 部署

`backend.yaml` 固定一个 Pod，包含 control（8001）和 gateway（8000），使用 Recreate 更新。分别写 `/data/control/control.sqlite` 与 `/data/gateway/gateway.sqlite`；仅 CU 挂 Kubernetes token。

Service `container-up:8000` 指向 CU，`channel-gateway:8000` 指向 Gateway。外部 NodePort 指向 Gateway。桶由 CU 动态创建，容器直接执行 `opencode serve`；原生端口只用于集群内 CU/Gateway 访问，不创建公开 Service。

Web 代理的 `GATEWAY_BASE_URL` 和隐患专家 MCP 的 `CONTAINER_UP_BASE_URL` 都指向 Gateway。后者保留调用端原有配置名。

## 从旧执行适配服务升级

暂停入站，完成或取消旧请求，并排空旧服务的可靠队列与跨桶迁移。停止旧 CU/Gateway 和所有桶，等待 Pod 退出，备份 state 卷。原生状态目录继续沿用 `/data/runtime/<frontend>/<bucket>/opencode-home`，保留用户 workspace。

同步更新源码、插件、镜像及 frontends.json：把每条 frontend 的 runtime 改为 bucket，删除 event_forwarding、control_url、rpc_port、health_port，bucket.gateway_url 使用 HTTP Gateway 地址。旧 limits.output_bytes 删除；请求超时仅保留在 control.limits.request_seconds，bucket.limits 只保留 file_bytes/spool_bytes。镜像使用 `Dockerfile.opencode` 构建的 `agent-opencode:opencode-2.3.2-bwrap011`。

CU 启动时只对已经排空的旧控制记录执行一次升级，给已有桶生成原生认证信息并标记为休眠。桶再次启动时 CU 更新 Deployment 为原生命令。已有原生会话的旧身份 metadata 会通过原生 API 转成 managed；历史正文、会话 ID 和工作区不变。旧桶内的 runtime.sqlite 已不参与运行，可在核对备份与历史后由运维清理。

不要混跑新旧进程：没有旧 WebSocket/RPC 入口，也没有兼容转发服务。已使用独立 Gateway 数据库的部署不需要迁移渠道记录。更早的 CU 内置渠道版本仍可在停服排空后使用 `python -m channel_gateway.migrate --source ... --destination ...` 导入历史投递记录。
