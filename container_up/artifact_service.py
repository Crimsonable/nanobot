"""Object storage and safe inbound downloads, owned solely by container_up."""
from __future__ import annotations

import asyncio
import ipaddress
import mimetypes
import socket
from pathlib import Path
from urllib.parse import quote, urlparse

import aiohttp
from aiohttp.abc import AbstractResolver

from agent_backend.config import ControlConfig
from agent_backend.journal import Journal
from agent_backend.models import Attachment, Identity, digest
from agent_backend.transfers import file_hash


class GuardedResolver(AbstractResolver):
    def __init__(self, allowed_cidrs: list[str]):
        self.networks = [ipaddress.ip_network(c) for c in allowed_cidrs]

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        records = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM)
        result = []
        for af, _, proto, _, address in records:
            ip = ipaddress.ip_address(address[0])
            if not ip.is_global and not any(ip in n for n in self.networks):
                raise ValueError("attachment target address is not permitted")
            result.append({"hostname": host, "host": str(ip), "port": port,
                           "family": af, "proto": proto, "flags": socket.AI_NUMERICHOST})
        return result

    async def close(self) -> None:
        pass


class ArtifactService:
    def __init__(self, config: ControlConfig, journal: Journal):
        self.config, self.journal = config, journal
        self.root = config.state_root / "incoming"
        self.root.mkdir(parents=True, exist_ok=True)
        self.download_locks: dict[str, asyncio.Lock] = {}
        self.reserved_bytes = 0

    def client(self):
        import boto3
        from botocore.config import Config
        storage = self.config.storage
        return boto3.client("s3", endpoint_url=storage.endpoint,
                            aws_access_key_id=storage.access_key_file.read_text().strip(),
                            aws_secret_access_key=storage.secret_key_file.read_text().strip(),
                            region_name=storage.region, config=Config(signature_version="s3v4"))

    def url(self, key: str) -> str:
        storage = self.config.storage
        if storage.public_base_url:
            return storage.public_base_url.rstrip("/") + "/" + quote(key)
        return self.client().generate_presigned_url("get_object", Params={
            "Bucket": storage.bucket, "Key": key}, ExpiresIn=storage.expiry_seconds)

    async def upload(self, identity: Identity, upload_id: str, path: Path,
                     attachment: Attachment) -> Attachment:
        key = "/".join([self.config.storage.prefix.strip("/"), identity.key,
                        digest(upload_id), attachment.sha256, attachment.filename])
        existing = self.journal.get("artifact", upload_id)
        if existing:
            if existing["owner"] != identity.key or existing["attachment"]["sha256"] != attachment.sha256:
                raise ValueError("upload identity mismatch")
            result = Attachment.model_validate(existing["attachment"])
            return result.model_copy(update={"url": await asyncio.to_thread(self.url, result.object_key)})
        await asyncio.to_thread(self.client().upload_file, str(path), self.config.storage.bucket,
                                key, ExtraArgs={"ContentType": attachment.content_type})
        result = attachment.model_copy(update={"url": await asyncio.to_thread(self.url, key),
                                                "storage": "minio", "object_key": key})
        self.journal.put("artifact", upload_id, {"owner": identity.key,
                         "attachment": result.model_dump(mode="json")})
        return result

    async def download(self, identity: Identity, attachment: Attachment) -> tuple[Attachment, Path]:
        key = digest([identity.key, attachment.attachment_id])
        async with self.download_locks.setdefault(key, asyncio.Lock()):
            used = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
            if used + self.reserved_bytes + self.config.limits.file_bytes > self.config.limits.spool_bytes:
                raise RuntimeError("SPOOL_FULL")
            self.reserved_bytes += self.config.limits.file_bytes
            try:
                return await self._download(identity, attachment)
            finally:
                self.reserved_bytes -= self.config.limits.file_bytes

    async def _download(self, identity: Identity, attachment: Attachment) -> tuple[Attachment, Path]:
        path = self.root / digest([identity.key, attachment.attachment_id])
        cached = self.journal.get("input", str(path.name))
        if cached and cached["url"] == attachment.url and path.exists():
            return Attachment.model_validate(cached["attachment"]), path
        resolver = GuardedResolver(self.config.download_private_cidrs)
        url = attachment.url
        # IP literals bypass aiohttp's resolver: validate them explicitly as well.
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=resolver),
                                        trust_env=False, timeout=aiohttp.ClientTimeout(total=120)) as client:
            try:
                for _ in range(6):
                    parsed = urlparse(url)
                    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                            or parsed.hostname not in self.config.download_hosts):
                        raise ValueError("attachment URL host is not configured")
                    await resolver.resolve(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
                    async with client.get(url, allow_redirects=False) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            from urllib.parse import urljoin
                            url = urljoin(url, response.headers["Location"])
                            continue
                        response.raise_for_status()
                        size = 0
                        with path.open("wb") as stream:
                            async for chunk in response.content.iter_chunked(256 * 1024):
                                size += len(chunk)
                                if size > self.config.limits.file_bytes:
                                    raise ValueError("attachment exceeds file limit")
                                stream.write(chunk)
                        result = attachment.model_copy(update={"size": size,
                            "sha256": await asyncio.to_thread(file_hash, path),
                            "content_type": attachment.content_type or
                            mimetypes.guess_type(attachment.filename)[0] or "application/octet-stream"})
                        self.journal.put("input", path.name, {"url": attachment.url,
                                          "attachment": result.model_dump(mode="json")})
                        return result, path
                raise ValueError("too many attachment redirects")
            except BaseException:
                path.unlink(missing_ok=True)
                raise
