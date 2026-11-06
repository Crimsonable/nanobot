"""Upload once using the registered frontend's channel and persist its reference."""
from __future__ import annotations

import asyncio
import hashlib
import io
from urllib.parse import quote, urlparse

from agent_backend.models import Attachment, Identity, digest


class ArtifactService:
    def __init__(self, config, journal, channels):
        self.config, self.journal, self.channels = config, journal, channels
        self.locks: dict[str, asyncio.Lock] = {}

    def client(self, storage):
        import boto3
        from botocore.config import Config
        return boto3.client("s3", endpoint_url=storage.endpoint,
            aws_access_key_id=storage.credential("access_key"),
            aws_secret_access_key=storage.credential("secret_key"),
            region_name=storage.region, config=Config(signature_version="s3v4"))

    @staticmethod
    def key(frontend_id, request_id, attachment_id):
        return digest([frontend_id, request_id, attachment_id])

    async def upload(self, row, attachment_id, data, filename, content_type, sha256):
        identity = Identity.model_validate(row["submit"]["identity"])
        frontend = self.config.frontends[identity.frontend_id]
        attachment = Attachment(attachment_id=attachment_id, filename=filename,
            content_type=content_type, size=len(data), sha256=hashlib.sha256(data).hexdigest())
        if attachment.sha256 != sha256:
            raise ValueError("attachment checksum mismatch")
        key = self.key(identity.frontend_id, row["request_id"], attachment_id)
        async with self.locks.setdefault(key, asyncio.Lock()):
            existing = self.journal.get("artifact", key)
            if existing:
                if existing["identity_id"] != identity.identity_id or existing["input"] != attachment.model_dump(mode="json"):
                    raise ValueError("attachment ID already used for different content")
                return Attachment.model_validate(existing["attachment"])
            if frontend.provider == "web":
                storage = frontend.attachment_storage
                if storage is None:
                    raise RuntimeError("attachment_storage is not configured for this Web frontend")
                object_key = "/".join([storage.key_prefix.strip("/"), identity.identity_id,
                                      key, attachment.sha256, attachment.filename])

                def upload_minio():
                    client = self.client(storage)
                    client.upload_fileobj(io.BytesIO(data), storage.bucket, object_key,
                                          ExtraArgs={"ContentType": attachment.content_type})
                    url = (storage.public_base_url.rstrip("/") + "/" + quote(object_key)
                           if storage.public_base_url else client.generate_presigned_url("get_object",
                               Params={"Bucket": storage.bucket, "Key": object_key},
                               ExpiresIn=storage.presign_expiry_seconds))
                    return {"url": url, "storage": "minio", "object_key": object_key}

                reference = await asyncio.to_thread(upload_minio)
            else:
                reference = await self.channels.parsers[identity.frontend_id].upload_outbound_attachment(
                    data=data, filename=filename, content_type=content_type)
            result = attachment.model_copy(update=reference)
            self.validate_reference(frontend.provider, identity.frontend_id, result)
            self.journal.put("artifact", key, {"identity_id": identity.identity_id,
                "input": attachment.model_dump(mode="json"), "attachment": result.model_dump(mode="json")})
            return result

    @staticmethod
    def validate_reference(provider, frontend_id, attachment):
        if provider == "web":
            if urlparse(attachment.url).scheme not in {"http", "https"}:
                raise ValueError("published Web attachment requires an object URL")
        elif attachment.storage != provider or attachment.frontend_id != frontend_id:
            raise ValueError("attachment channel does not match the request")
        elif provider == "qxt" and not attachment.media_id.strip():
            raise ValueError("QXT attachment requires media_id")
        elif provider == "feishu" and not (attachment.image_key if attachment.upload_type == "image" else attachment.file_key):
            raise ValueError("Feishu attachment requires a media key")

    def validate_result(self, row, attachments):
        frontend_id = row["frontend_id"]
        provider = self.config.frontends[frontend_id].provider
        for attachment in attachments:
            self.validate_reference(provider, frontend_id, attachment)
            if provider != "web":
                key = self.key(frontend_id, row["request_id"], attachment.attachment_id)
                existing = self.journal.get("artifact", key)
                if not existing or existing["attachment"] != attachment.model_dump(mode="json"):
                    raise ValueError("media reference was not published for this request")
