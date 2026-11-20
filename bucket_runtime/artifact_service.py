"""Publish workspace snapshots through Gateway; BU does not own channel uploads."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from agent_backend.config import RuntimeConfig
from agent_backend.models import Attachment, Submit

log = logging.getLogger(__name__)


class ArtifactService:
    def __init__(self, config: RuntimeConfig):
        self.config = config

    def client(self):
        target = urlsplit(self.config.gateway_url)
        base = urlunsplit(("https" if target.scheme == "wss" else "http", target.netloc, "", "", ""))
        return httpx.AsyncClient(base_url=base, timeout=180, trust_env=False)

    async def upload(self, submit: Submit, attachment_id: str, path: Path,
                     attachment: Attachment) -> Attachment:
        if submit.identity.frontend_id != self.config.frontend_id:
            raise PermissionError("attachment belongs to another frontend")

        async def chunks():
            with path.open("rb") as source:
                while chunk := await asyncio.to_thread(source.read, 256 * 1024):
                    yield chunk

        route = "/internal/attachments/" + "/".join(quote(value, safe="") for value in (
            submit.identity.frontend_id, submit.request_id, attachment_id))
        async with self.client() as client:
            response = await client.post(route, content=chunks(),
                params={"filename": attachment.filename, "content_type": attachment.content_type,
                        "sha256": attachment.sha256},
                headers={"x-bucket-id": self.config.bucket_id, "content-type": "application/octet-stream"})
            log.log(logging.WARNING if response.is_error else logging.INFO,
                    "attachment_upload stage=runtime event=gateway_response frontend_id=%s request_id=%s attachment_id=%s file_bytes=%d http_status=%d",
                    submit.identity.frontend_id, submit.request_id, attachment_id, attachment.size, response.status_code)
            response.raise_for_status()
            result = Attachment.model_validate(response.json())
        if (result.attachment_id != attachment_id or result.sha256 != attachment.sha256
                or result.size != attachment.size or result.filename != attachment.filename):
            raise ValueError("Gateway returned a different attachment")
        return result
