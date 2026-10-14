"""MinIO publication backend for the publish_attachment tool, inside the bucket."""
from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import quote

from agent_backend.config import RuntimeConfig
from agent_backend.journal import Journal
from agent_backend.models import Attachment, Identity, digest


class ArtifactService:
    def __init__(self, config: RuntimeConfig, journal: Journal):
        self.config, self.journal = config, journal

    def client(self):
        import boto3
        from botocore.config import Config
        storage = self.config.storage
        if storage is None:
            raise RuntimeError("publish_attachment storage is not configured")
        return boto3.client("s3", endpoint_url=storage.endpoint,
                            aws_access_key_id=storage.credential("access_key"),
                            aws_secret_access_key=storage.credential("secret_key"),
                            region_name=storage.region, config=Config(signature_version="s3v4"))

    def url(self, key: str) -> str:
        storage = self.config.storage
        if storage is None:
            raise RuntimeError("publish_attachment storage is not configured")
        if storage.public_base_url:
            return storage.public_base_url.rstrip("/") + "/" + quote(key)
        return self.client().generate_presigned_url("get_object", Params={
            "Bucket": storage.bucket, "Key": key}, ExpiresIn=storage.presign_expiry_seconds)

    async def upload(self, identity: Identity, attachment_id: str, path: Path,
                     attachment: Attachment) -> Attachment:
        if self.config.storage is None:
            raise RuntimeError("publish_attachment storage is not configured")
        key = "/".join([self.config.storage.key_prefix.strip("/"), identity.identity_id,
                        digest(attachment_id), attachment.sha256, attachment.filename])
        existing = self.journal.get("artifact", attachment_id)
        if existing:
            if existing["identity_id"] != identity.identity_id or existing["attachment"]["sha256"] != attachment.sha256:
                raise ValueError("upload identity mismatch")
            result = Attachment.model_validate(existing["attachment"])
            return result.model_copy(update={"url": await asyncio.to_thread(self.url, result.object_key)})
        await asyncio.to_thread(self.client().upload_file, str(path), self.config.storage.bucket,
                                key, ExtraArgs={"ContentType": attachment.content_type})
        result = attachment.model_copy(update={"url": await asyncio.to_thread(self.url, key),
                                                "storage": "minio", "object_key": key})
        self.journal.put("artifact", attachment_id, {"identity_id": identity.identity_id,
                         "attachment": result.model_dump(mode="json")})
        return result
