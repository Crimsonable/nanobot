from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Annotated, Any

import httpx
import uvicorn
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.responses import JSONResponse

from hazard_expert_mcp.config import Settings
from hazard_expert_mcp.history import AnalysisHistory
from hazard_expert_mcp.service import (
    AnalysisResult,
    ExpertService,
    Identifier,
    Query,
    UploadResult,
    make_s3_client,
)


class HealthAccessFilter(logging.Filter):
    """Suppress successful probes without hiding failed probes or business traffic."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "uvicorn.access" or not isinstance(record.args, tuple):
            return True
        if len(record.args) != 5:
            return True
        _, method, path, _, status = record.args
        if not isinstance(path, str) or not isinstance(status, int):
            return True
        return not (
            method in {"GET", "HEAD"}
            and path.split("?", 1)[0].rstrip("/") == "/healthz"
            and 200 <= status < 400
        )


def logging_config() -> dict:
    config = deepcopy(uvicorn.config.LOGGING_CONFIG)
    config["filters"] = {"health_access": {"()": HealthAccessFilter}}
    config["handlers"]["access"]["filters"] = ["health_access"]
    return config


class MCPRequestMiddleware:
    """Shared service credential for trusted callers; user_id is supplied by that caller."""

    def __init__(self, app, api_key: str, max_request_bytes: int):
        self.app = app
        self.api_key = api_key
        self.max_request_bytes = max_request_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].rstrip("/") != "/mcp":
            await self.app(scope, receive, send)
            return
        if self.api_key:
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode("latin-1")
            scheme, _, token = auth.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(
                token.encode("utf-8"), self.api_key.encode("utf-8")
            ):
                await JSONResponse(
                    {"detail": "Invalid or missing bearer token"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
                return
        if scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_request_bytes:
                await JSONResponse({"detail": "Request body too large"}, status_code=413)(
                    scope, receive, send
                )
                return
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(settings: Settings | None = None, service: ExpertService | None = None) -> FastAPI:
    settings = settings or Settings()
    active_service = service
    history = None
    mcp = FastMCP(
        "hazard-expert-mcp",
        instructions=(
            "提供附件上传、隐患识别与分析、分析结果查询三个工具。user_id 必须由调用方使用当前登录用户的真实 ID。"
            "同一用户多轮对话须复用 chat_id；新会话使用新的 chat_id。"
            f"调用 hazard_analysis_expert 前，调用方须将工具调用总超时和 HTTP 读取超时均设为至少 {settings.container_up_timeout_seconds + 30:g} 秒，"
            f"以覆盖本服务的后端响应等待超时（{settings.container_up_timeout_seconds:g} 秒）及转发开销；反向代理超时也需相应配置。"
            "调用超时或断开后，不要重复提交分析；使用相同 user_id 和 chat_id 调用 query_analysis_result 查询。"
            "同一 user_id 和 chat_id 的结果轮询至少间隔 15 秒，不要连续或并发查询。"
            "仅当 running（处理中）时，等待至少 15 秒后再次查询；completed 时停止轮询并读取最新记录 result；"
            "unknown 表示未收到最终答案，not_found 表示无本地记录，这两种状态应停止自动轮询，不能当作分析成功，也不要自动重提。"
            "附件先上传取得 URL，再传入专家工具；不要把 Base64 内容传给专家工具。"
        ),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )

    @mcp.tool(
        name="upload_attachment",
        description=(
            "将附件内容上传到 MinIO，返回可下载的临时 URL。"
            "user_id 使用当前用户 ID；content_base64 是文件字节的标准 Base64 编码。"
            "返回的 url 可直接作为 hazard_analysis_expert 的 attachments 元素。"
        ),
        annotations=ToolAnnotations(
            title="上传附件至 MinIO",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
        ),
    )
    async def upload_attachment(
        user_id: Identifier,
        filename: Annotated[str, Field(description="文件名，含扩展名，不含目录")],
        content_base64: Annotated[str, Field(description="文件内容的标准 Base64，不带 data: 前缀")],
        content_type: Annotated[
            str | None, Field(description="MIME 类型；省略时按文件名推断")
        ] = None,
    ) -> UploadResult:
        assert active_service is not None
        return await active_service.upload(user_id, filename, content_base64, content_type)

    @mcp.tool(
        name="hazard_analysis_expert",
        description=(
            "隐患识别与分析专家：根据用户问题和图片、视频或文档附件 URL，"
            "调用用户绑定的 nanobot 分析现场隐患、风险和整改措施。"
            "user_id 必须是当前用户 ID；复用相同 chat_id 继续多轮对话，"
            "切换 chat_id 开启独立会话。attachments 无附件时可省略。"
            f"此工具同步等待最终分析结果，调用方的工具调用总超时和 HTTP 读取超时均须至少为 {settings.container_up_timeout_seconds + 30:g} 秒；"
            f"本服务等待后端响应的超时配置为 {settings.container_up_timeout_seconds:g} 秒。"
            "这些说明不会自动修改客户端超时，调用方需在客户端配置，反向代理超时也需相应调大。"
            "调用方超时或断开后，本服务会继续等待并保存结果；请用相同 user_id、chat_id 调用 query_analysis_result，勿重复提交。"
            "如果上游请求本身超时、返回错误或 MCP 进程中断，只能查询到结果未知记录，无法保证取得最终答案。"
        ),
        annotations=ToolAnnotations(
            title="隐患识别与分析专家",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
        ),
    )
    async def hazard_analysis_expert(
        user_id: Identifier,
        chat_id: Identifier,
        query: Query,
        attachments: Annotated[
            list[str] | None, Field(description="附件 http(s) URL 列表，最多 20 个")
        ] = None,
    ) -> AnalysisResult:
        assert active_service is not None
        assert history is not None
        return await history.analyze(active_service, user_id, chat_id, query, attachments or [])

    @mcp.tool(
        name="query_analysis_result",
        description=(
            "按当前用户 user_id 和 chat_id 查询本 MCP 保存的分析消息记录，无需再次提交分析。"
            "hazard_analysis_expert 超时或断开后优先调用本工具；默认返回最近 20 轮，按提交时间倒序。"
            "检查最新记录的 query、request_id 和 created_at，确认是目标轮次，不要使用旧轮结果替代。"
            "同一 user_id 和 chat_id 的结果轮询至少间隔 15 秒，不要连续或并发查询。"
            "running：处理中，等待至少 15 秒后再查；completed：停止轮询，result 包含最终回答；"
            "unknown：上游超时/错误或服务中断，未取得最终结果，请人工核查，勿自动重复提交；"
            "not_found：本地没有对应记录。unknown 和 not_found 均应停止自动轮询。"
            "仅能查询经此 MCP 提交的分析，不读取后端其他会话。"
            "存在 next_before_id 时，将其作为 before_id 查询更早记录。"
        ),
        annotations=ToolAnnotations(
            title="查询隐患分析结果", readOnlyHint=True, destructiveHint=False,
            idempotentHint=True,
        ),
    )
    async def query_analysis_result(
        user_id: Identifier,
        chat_id: Identifier,
        limit: Annotated[int, Field(ge=1, le=100, description="返回的最近记录数")] = 20,
        before_id: Annotated[int | None, Field(ge=1, description="分页游标，首次查询省略")] = None,
    ) -> dict[str, Any]:
        assert history is not None
        return history.query(user_id, chat_id, limit, before_id)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal active_service, history
        history = AnalysisHistory(settings.history_db_path)
        if service is not None:
            try:
                async with mcp.session_manager.run():
                    yield
            finally:
                await history.close()
            return
        storage = make_s3_client(settings, settings.minio_endpoint)
        signer = make_s3_client(
            settings, settings.minio_download_endpoint or settings.minio_endpoint
        )
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    settings.container_up_timeout_seconds,
                    connect=settings.container_up_connect_timeout_seconds,
                ),
                follow_redirects=False,
            ) as http:
                active_service = ExpertService(settings, http, storage, signer)
                try:
                    async with mcp.session_manager.run():
                        yield
                finally:
                    await history.close()
        finally:
            active_service = None
            storage.close()
            signer.close()

    app = FastAPI(
        title="隐患识别与分析 MCP",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        MCPRequestMiddleware,
        api_key=settings.mcp_api_key.get_secret_value(),
        max_request_bytes=settings.max_request_bytes,
    )

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    app.mount("/", mcp.streamable_http_app())
    return app


def main() -> None:
    settings = Settings()
    uvicorn.run(
        create_app(settings),
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level=settings.log_level,
        log_config=logging_config(),
    )


if __name__ == "__main__":
    main()
