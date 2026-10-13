from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import mimetypes
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, StringConstraints

from hazard_expert_mcp.config import Settings

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=256, pattern=r"^\S+$")]
Query = Annotated[str, StringConstraints(min_length=1, max_length=100000)]


class UploadResult(BaseModel):
    url: str
    filename: str
    content_type: str
    size_bytes: int
    bucket: str
    object_key: str
    expires_at: str


class AnalysisResult(BaseModel):
    status: str
    user_id: str
    chat_id: str
    content: str
    attachments: list[Any] = Field(default_factory=list)


def make_s3_client(settings: Settings, endpoint: str) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.minio_access_key.get_secret_value(),
        aws_secret_access_key=settings.minio_secret_key.get_secret_value(),
        region_name=settings.minio_region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=settings.minio_connect_timeout_seconds,
            read_timeout=settings.minio_read_timeout_seconds,
            retries={"mode": "standard", "total_max_attempts": 3},
        ),
    )


class ExpertService:
    def __init__(self, settings: Settings, http: httpx.AsyncClient, storage: Any, signer: Any):
        self.settings = settings
        self.http = http
        self.storage = storage
        self.signer = signer

    async def upload(
        self, user_id: str, filename: str, content_base64: str, content_type: str | None
    ) -> UploadResult:
        # Reject oversized content before allocating a second, decoded copy.
        if len(content_base64) > 4 * ((self.settings.max_upload_bytes + 2) // 3):
            raise ToolError("附件超过 MAX_UPLOAD_BYTES 限制。")
        try:
            data = base64.b64decode(content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ToolError("content_base64 必须是标准 Base64，不包含 data: 前缀。") from None
        if not data:
            raise ToolError("附件内容不能为空。")
        if len(data) > self.settings.max_upload_bytes:
            raise ToolError("附件超过 MAX_UPLOAD_BYTES 限制。")
        if (
            not filename
            or len(filename) > 255
            or filename in {".", ".."}
            or any(ch in filename for ch in ("/", "\\"))
            or any(ord(ch) < 32 for ch in filename)
        ):
            raise ToolError("filename 必须是文件名，不能包含目录或控制字符，最长 255 字符。")
        mime = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if len(mime) > 255 or "/" not in mime or any(ord(ch) < 32 for ch in mime):
            raise ToolError("content_type 必须是有效的 MIME 类型。")

        # Hash the opaque user ID without lossy path sanitization; unique names never overwrite.
        owner = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc)
        object_key = "/".join(
            filter(
                None,
                [
                    self.settings.minio_key_prefix.strip("/"),
                    owner,
                    now.strftime("%Y/%m/%d"),
                    f"{uuid4().hex}-{filename}",
                ],
            )
        )

        def put_and_sign() -> str:
            self.storage.put_object(
                Bucket=self.settings.minio_bucket,
                Key=object_key,
                Body=data,
                ContentType=mime,
            )
            # Sign against the actual download origin. Replacing the host afterwards breaks SigV4.
            return self.signer.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.settings.minio_bucket, "Key": object_key},
                ExpiresIn=self.settings.minio_presign_expiry_seconds,
            )

        try:
            url = await asyncio.to_thread(put_and_sign)
        except (BotoCoreError, ClientError):
            raise ToolError("MinIO 上传失败，请检查连接、桶及读写权限。") from None
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self.settings.minio_presign_expiry_seconds
        )
        return UploadResult(
            url=url,
            filename=filename,
            content_type=mime,
            size_bytes=len(data),
            bucket=self.settings.minio_bucket,
            object_key=object_key,
            expires_at=expires_at.isoformat(),
        )

    async def analyze(
        self, user_id: str, chat_id: str, query: str, attachments: list[str]
    ) -> AnalysisResult:
        if not query.strip():
            raise ToolError("query 不能为空。")
        if len(attachments) > 20:
            raise ToolError("每次最多传入 20 个附件 URL。")
        for url in attachments:
            try:
                parsed = urlsplit(url)
                valid = (
                    parsed.scheme in {"http", "https"}
                    and parsed.hostname
                    and parsed.username is None
                    and parsed.password is None
                    and not any(ch.isspace() for ch in url)
                )
            except ValueError:
                valid = False
            if not valid:
                raise ToolError("附件必须是 http(s) URL，不能是本地路径或包含账号密码。")

        endpoint = (
            f"{self.settings.container_up_base_url}/inbound-sync/"
            f"{quote(self.settings.container_up_frontend_id, safe='')}"
        )
        headers = {}
        if token := self.settings.container_up_api_key.get_secret_value():
            headers["Authorization"] = f"Bearer {token}"
        try:
            # Deliberately no retries: another POST could run the same agent task twice.
            response = await self.http.post(
                endpoint,
                headers=headers,
                json={
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "content": query,
                    "attachments": attachments,
                },
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            raise ToolError("等待隐患分析结果超时；后端可能仍在执行，请勿自动重复提交。") from None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 504:
                raise ToolError(
                    "container-up 等待回复超时；后端可能仍在执行，请勿自动重复提交。"
                ) from None
            raise ToolError(f"container-up 返回 HTTP {status}，请检查服务端日志。") from None
        except httpx.RequestError:
            raise ToolError(
                "container-up 连接异常；请求是否已执行未知，请检查服务端状态。"
            ) from None

        try:
            payload = response.json()
        except ValueError:
            raise ToolError("container-up 返回了无效 JSON。") from None
        if (
            not isinstance(payload, dict)
            or payload.get("status") != "completed"
            or payload.get("user_id") != user_id
            or payload.get("chat_id") != chat_id
            or payload.get("frontend_id") != self.settings.container_up_frontend_id
            or not isinstance(payload.get("content"), str)
            or not isinstance(payload.get("attachments", []), list)
        ):
            raise ToolError("container-up 回复格式或用户/会话标识不匹配。")
        return AnalysisResult(
            status="completed",
            user_id=user_id,
            chat_id=chat_id,
            content=payload["content"],
            attachments=payload.get("attachments", []),
        )
