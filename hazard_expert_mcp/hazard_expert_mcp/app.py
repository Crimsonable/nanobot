from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
import uvicorn
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.responses import JSONResponse

from hazard_expert_mcp.config import Settings
from hazard_expert_mcp.service import (
    AnalysisResult,
    ExpertService,
    Identifier,
    Query,
    UploadResult,
    make_s3_client,
)


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
    mcp = FastMCP(
        "hazard-expert-mcp",
        instructions=(
            "提供附件上传和隐患识别与分析两个工具。user_id 必须由调用方使用当前登录用户的真实 ID。"
            "同一用户多轮对话须复用 chat_id；新会话使用新的 chat_id。"
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
        return await active_service.analyze(user_id, chat_id, query, attachments or [])

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal active_service
        if service is not None:
            async with mcp.session_manager.run():
                yield
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
                async with mcp.session_manager.run():
                    yield
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
    )


if __name__ == "__main__":
    main()
