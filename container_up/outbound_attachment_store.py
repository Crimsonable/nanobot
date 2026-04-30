from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from container_up.attachment_paths import normalize_outbound_attachments
from container_up.frontend_config import safe_frontend_id
from container_up.settings import (
    MINIO_ACCESS_KEY,
    MINIO_BUCKET,
    MINIO_ENDPOINT,
    MINIO_KEY_PREFIX,
    MINIO_PRESIGN_EXPIRY_SECONDS,
    MINIO_PUBLIC_BASE_URL,
    MINIO_REGION,
    MINIO_SECRET_KEY,
)
from container_up.workspace_manager import safe_user_id


@dataclass(frozen=True)
class MinioConfig:
    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    region: str
    public_base_url: str
    key_prefix: str
    presign_expiry_seconds: int


def _storage_config(frontend_config: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(frontend_config or {})
    storage = raw.get("attachment_storage")
    if isinstance(storage, dict):
        return storage
    return raw


def _string_config(raw: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    return default


def _int_config(raw: dict[str, Any], *keys: str, default: int) -> int:
    for key in keys:
        value = str(raw.get(key) or "").strip()
        if not value:
            continue
        try:
            return int(value)
        except ValueError:
            continue
    return default


def minio_config_from_frontend(frontend_config: dict[str, Any] | None) -> MinioConfig | None:
    raw = _storage_config(frontend_config)
    provider = _string_config(raw, "provider", "storage_provider")
    if provider and provider.lower() != "minio":
        return None

    endpoint = _string_config(raw, "endpoint", "minio_endpoint", default=MINIO_ENDPOINT)
    access_key = _string_config(
        raw,
        "access_key",
        "minio_access_key",
        default=MINIO_ACCESS_KEY,
    )
    secret_key = _string_config(
        raw,
        "secret_key",
        "minio_secret_key",
        default=MINIO_SECRET_KEY,
    )
    bucket = _string_config(raw, "bucket", "minio_bucket", default=MINIO_BUCKET)
    if not endpoint or not access_key or not secret_key or not bucket:
        return None

    return MinioConfig(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        region=_string_config(raw, "region", "minio_region", default=MINIO_REGION),
        public_base_url=_string_config(
            raw,
            "public_base_url",
            "minio_public_base_url",
            default=MINIO_PUBLIC_BASE_URL,
        ).rstrip("/"),
        key_prefix=_string_config(raw, "key_prefix", "minio_key_prefix", default=MINIO_KEY_PREFIX).strip("/"),
        presign_expiry_seconds=max(
            1,
            _int_config(
                raw,
                "presign_expiry_seconds",
                "minio_presign_expiry_seconds",
                default=MINIO_PRESIGN_EXPIRY_SECONDS,
            ),
        ),
    )


class MinioAttachmentStore:
    def __init__(self, config: MinioConfig) -> None:
        self.config = config

    async def upload_attachment(
        self,
        attachment: Any,
        *,
        frontend_id: str,
        user_id: str,
    ) -> Any:
        path, original = self._local_attachment_path(attachment)
        if path is None:
            return attachment

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        object_key = self._object_key(
            path=path,
            frontend_id=frontend_id,
            user_id=user_id,
        )
        await asyncio.to_thread(
            self._upload_file_sync,
            path,
            object_key,
            content_type,
        )
        url = await asyncio.to_thread(self._resolve_object_url_sync, object_key)
        payload = {
            "url": url,
            "filename": path.name,
            "content_type": content_type,
            "storage": "minio",
            "bucket": self.config.bucket,
            "object_key": object_key,
        }
        if isinstance(original, dict):
            merged = dict(original)
            merged.update(payload)
            return merged
        return payload

    def _upload_file_sync(self, path: Path, object_key: str, content_type: str) -> None:
        client = self._build_client()
        extra_args = {"ContentType": content_type}
        client.upload_file(
            str(path),
            self.config.bucket,
            object_key,
            ExtraArgs=extra_args,
        )

    def _resolve_object_url_sync(self, object_key: str) -> str:
        if self.config.public_base_url:
            return f"{self.config.public_base_url}/{quote(object_key)}"
        client = self._build_client()
        return str(
            client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.config.bucket, "Key": object_key},
                ExpiresIn=self.config.presign_expiry_seconds,
            )
        )

    def _build_client(self) -> Any:
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=self.config.endpoint,
            aws_access_key_id=self.config.access_key,
            aws_secret_access_key=self.config.secret_key,
            region_name=self.config.region or None,
        )

    def _object_key(self, *, path: Path, frontend_id: str, user_id: str) -> str:
        stat = path.stat()
        digest_input = (
            f"{path.resolve(strict=False)}:{stat.st_size}:{stat.st_mtime_ns}"
        ).encode("utf-8")
        digest = hashlib.sha1(digest_input).hexdigest()
        suffix = path.suffix.lower()
        filename = f"{digest}{suffix}" if suffix else digest
        parts = [
            self.config.key_prefix,
            safe_frontend_id(frontend_id),
            safe_user_id(user_id),
            filename,
        ]
        return "/".join(part for part in parts if part)

    @staticmethod
    def _local_attachment_path(attachment: Any) -> tuple[Path | None, Any]:
        original = attachment
        if isinstance(attachment, dict):
            ref = str(attachment.get("url") or "").strip()
        else:
            ref = str(attachment or "").strip()
        if not ref or ref.startswith(("http://", "https://")):
            return None, original

        path = Path(ref).expanduser()
        if not path.is_absolute() or not path.is_file():
            return None, original
        return path, original


async def prepare_outbound_attachments(
    attachments: list[Any] | None,
    *,
    frontend_id: str,
    user_id: str,
    frontend_config: dict[str, Any] | None = None,
) -> list[Any]:
    normalized = normalize_outbound_attachments(
        list(attachments or []),
        frontend_id=frontend_id,
    )
    config = minio_config_from_frontend(frontend_config)
    if config is None:
        return normalized

    store = MinioAttachmentStore(config)
    prepared: list[Any] = []
    for attachment in normalized:
        try:
            prepared.append(
                await store.upload_attachment(
                    attachment,
                    frontend_id=frontend_id,
                    user_id=user_id,
                )
            )
        except Exception as exc:
            raise RuntimeError(
                "failed to upload outbound attachment "
                f"frontend_id={frontend_id} user_id={user_id} "
                f"endpoint={config.endpoint} bucket={config.bucket} "
                f"attachment={attachment!r}: {exc}"
            ) from exc
    return prepared
